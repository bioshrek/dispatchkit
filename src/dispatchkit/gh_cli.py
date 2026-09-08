"""`gh`-CLI adapter for the `GitHubApi` port (D3).

Split into pure parts and a thin subprocess shim so most of it is testable
offline: `parse_state` and `parse_field_catalog` turn recorded API payloads
into value objects, the `*_command` helpers build argv lists, and `_run` is the
only function that actually shells out.

Two rules the shim enforces:

- **Never a shell.** Every call is an argv list passed to `subprocess.run`
  without `shell=True`, so nothing from a graph file or an issue body can be
  interpolated into a command.
- **Bodies go over stdin.** Issue bodies are long, contain backticks and
  newlines, and would otherwise land in the process table.

Authentication is `gh`'s own credential — no PAT in a file, no token in argv.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from dispatchkit.board import SINGLE_SELECT, BoardSnapshot, FieldSpec
from dispatchkit.doctor import parse_labels, parse_project, parse_token_scopes
from dispatchkit.github import IssueState, RepoState
from dispatchkit.model import Checks, PullRequest

# The coding agent is a bot actor, so it cannot be assigned with
# `gh issue edit --add-assignee`: it has to be looked up by capability and
# handed over through a mutation.
AGENT_LOGIN = "copilot-swe-agent"

ACTOR_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    suggestedActors(capabilities: [CAN_BE_ASSIGNED], first: 100) {
      nodes { login __typename ... on Bot { id } ... on User { id } }
    }
  }
}
"""

# `replace`, not `add`: a pass that loses a race re-sends the same single
# actor, which is a no-op, rather than piling up assignees.
ASSIGN_MUTATION = """
mutation($assignableId: ID!, $actorIds: [ID!]!) {
  replaceActorsForAssignable(input: {assignableId: $assignableId, actorIds: $actorIds}) {
    assignable { ... on Issue { number } }
  }
}
"""

STATE_QUERY = """
query($owner: String!, $repo: String!, $first: Int!) {
  repository(owner: $owner, name: $repo) {
    issues(first: $first, labels: ["dispatchkit"], states: [OPEN, CLOSED]) {
      nodes {
        id
        number
        title
        body
        state
        labels(first: 20) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        timelineItems(first: 20, itemTypes: [CROSS_REFERENCED_EVENT]) {
          nodes {
            ... on CrossReferencedEvent {
              source {
                ... on PullRequest {
                  number
                  state
                  isDraft
                  mergeable
                  files(first: 100) { nodes { path } }
                  commits(last: 1) {
                    nodes {
                      commit {
                        checkSuites(first: 20) { nodes { status conclusion } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        projectItems(first: 5) {
          nodes {
            id
            fieldValues(first: 20) {
              nodes {
                __typename
                ... on ProjectV2ItemFieldTextValue {
                  text
                  field { ... on ProjectV2FieldCommon { name } }
                }
                ... on ProjectV2ItemFieldNumberValue {
                  number
                  field { ... on ProjectV2FieldCommon { name } }
                }
                ... on ProjectV2ItemFieldSingleSelectValue {
                  name
                  field { ... on ProjectV2FieldCommon { name } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def parse_state(payload: dict[str, Any]) -> RepoState:
    """Turn a recorded GraphQL response into a `RepoState`."""
    nodes = payload["data"]["repository"]["issues"]["nodes"]
    return RepoState(tuple(_parse_issue(node) for node in nodes))


def _parse_issue(node: dict[str, Any]) -> IssueState:
    items = node.get("projectItems", {}).get("nodes") or []
    item = items[0] if items else None
    return IssueState(
        number=int(node["number"]),
        title=node["title"],
        body=node["body"] or "",
        labels=tuple(label["name"] for label in node["labels"]["nodes"]),
        closed=node["state"] == "CLOSED",
        project_item_id=item["id"] if item else None,
        fields=_parse_fields(item) if item else {},
        assignees=tuple(user["login"] for user in (node.get("assignees", {}).get("nodes") or [])),
        open_prs=_parse_open_prs(node),
        node_id=node.get("id"),
    )


def _parse_open_prs(node: dict[str, Any]) -> tuple[PullRequest, ...]:
    """Open pull requests cross-referencing this issue, in the order recorded."""
    prs: list[PullRequest] = []
    for event in node.get("timelineItems", {}).get("nodes") or []:
        source = event.get("source") or {}
        if source.get("state") == "OPEN" and "number" in source:
            prs.append(
                PullRequest(
                    int(source["number"]),
                    _parse_checks(source),
                    draft=bool(source.get("isDraft")),
                    files=_parse_files(source),
                    mergeable=source.get("mergeable") == "MERGEABLE",
                )
            )
    return tuple(prs)


def _parse_files(source: dict[str, Any]) -> tuple[str, ...]:
    """The paths a pull request changes, for the blast-radius fence.

    Capped at 100 by the query. A pull request larger than that returns a
    partial list, which would be the one case where the fence could be walked
    past silently — so the cap is deliberately far above any task this tool is
    meant to dispatch, and a task that big is a decomposition failure.
    """
    nodes = (source.get("files") or {}).get("nodes") or []
    return tuple(node["path"] for node in nodes if node.get("path"))


def _parse_checks(source: dict[str, Any]) -> Checks:
    """CI's verdict on a PR's head commit, from its check suites.

    Read from `checkSuites` rather than `statusCheckRollup` because a run held
    for approval yields a suite with no check runs at all, and the rollup —
    which is assembled from check runs — is then `null`. The rollup cannot
    distinguish a stalled pipeline from a repository that has no CI.
    """
    commits = (source.get("commits") or {}).get("nodes") or []
    if not commits:
        return Checks.NONE
    suites = ((commits[0].get("commit") or {}).get("checkSuites") or {}).get("nodes") or []
    return Checks.combine(_suite_checks(suite) for suite in suites)


def _suite_checks(suite: dict[str, Any]) -> Checks:
    if suite.get("status") != "COMPLETED":
        return Checks.PENDING
    conclusion = suite.get("conclusion")
    if conclusion == "ACTION_REQUIRED":
        return Checks.BLOCKED
    # Anything not known to be benign counts against the pull request: GitHub
    # adds conclusions over time, and guessing green would auto-merge on a
    # verdict this code has never seen.
    return Checks.PASSING if conclusion in _BENIGN_CONCLUSIONS else Checks.FAILING


#: Suite conclusions that do not stand in the way of a merge.
_BENIGN_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})


def _parse_fields(item: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for value in item.get("fieldValues", {}).get("nodes") or []:
        name = (value.get("field") or {}).get("name")
        if not name:
            continue  # a field type we do not model; ignore rather than guess
        for key in ("text", "name", "number", "date"):
            if key in value and value[key] is not None:
                raw = value[key]
                fields[name] = str(int(raw)) if key == "number" else str(raw)
                break
    return fields


class ProjectFieldError(RuntimeError):
    """A board field or single-select option the project does not have.

    Its own type because it is a *configuration* failure, not an API failure:
    the fix is to run `dispatchkit init`, not to retry.
    """


@dataclass(frozen=True, slots=True)
class FieldCatalog:
    """Project (v2) field ids, and option ids for single-select fields."""

    ids: dict[str, str]
    options: dict[str, dict[str, str]]

    def option_id(self, field_name: str, value: str) -> str | None:
        return self.options.get(field_name, {}).get(value)

    def resolve(self, field_name: str, value: str) -> tuple[str, str | None]:
        """`(field id, option id or None)`, or raise naming what is missing.

        The silent version of this returned `None` for the option id and let
        the caller send `--text`, which `gh` rejects with a message about
        neither the field nor the value.
        """
        if field_name not in self.ids:
            raise ProjectFieldError(
                f"the project has no `{field_name}` field "
                f"(it has: {_names(self.ids)}); run `dispatchkit init` to create it"
            )
        if field_name not in self.options:
            return self.ids[field_name], None  # a text or number field
        option = self.options[field_name].get(value)
        if option is None:
            raise ProjectFieldError(
                f"the `{field_name}` field has no `{value}` option "
                f"(it has: {_names(self.options[field_name])}); "
                "run `dispatchkit init` to see what the board is missing"
            )
        return self.ids[field_name], option


def _names(mapping: dict[str, str]) -> str:
    return ", ".join(f"`{name}`" for name in sorted(mapping)) or "nothing"


def parse_field_catalog(payload: dict[str, Any]) -> FieldCatalog:
    ids: dict[str, str] = {}
    options: dict[str, dict[str, str]] = {}
    for entry in payload.get("fields", []):
        name = entry["name"]
        ids[name] = entry["id"]
        if entry.get("options"):
            options[name] = {option["name"]: option["id"] for option in entry["options"]}
    return FieldCatalog(ids, options)


def parse_agent_actor(payload: dict[str, Any]) -> str | None:
    """The coding agent's actor id, or `None` if the repo has no agent."""
    nodes = payload["data"]["repository"]["suggestedActors"]["nodes"]
    for node in nodes:
        if node.get("login") == AGENT_LOGIN:
            return str(node["id"])
    return None


def actor_command(owner: str, repo: str) -> list[str]:
    return [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={ACTOR_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"repo={repo}",
    ]


def ready_command(number: int, repo: str) -> list[str]:
    """Take a pull request out of draft.

    `gh pr ready` rather than the GraphQL mutation because the CLI already
    resolves a number to a node id against the right repository, and the
    number is the only thing the planner knows.
    """
    return ["gh", "pr", "ready", str(number), "--repo", repo]


def protection_command(repo: str, branch: str) -> list[str]:
    """Read a branch's protection. A 404 here means "not protected"."""
    return ["gh", "api", f"repos/{repo}/branches/{branch}/protection"]


def parse_protection(payload: dict[str, Any]) -> bool:
    """Is this branch protected in the way a `verify: auto` merge relies on?

    Only required *status checks* count. Required reviews protect a branch from
    a human's mistake; they do nothing about a red pull request, which is the
    failure this is meant to catch. An empty context list is protection that
    requires nothing, so it is not protection for this purpose.
    """
    checks = payload.get("required_status_checks") or {}
    return bool(checks.get("contexts"))


def merge_command(number: int, repo: str) -> list[str]:
    """Squash-merge a pull request. Direct, never `--auto`, never `--admin`.

    Both omissions are load-bearing and both were found by probing a live
    repository, because neither is documented:

    `--auto` reads as "merge once the requirements are met", but on a branch
    with no protection there are no requirements, and `gh` merges immediately —
    silently, exit 0, no checks consulted. The GraphQL mutation it is named
    after refuses that same case outright ("Auto merge is not allowed for this
    repository"), so the flag is strictly less safe than the thing it wraps.
    A direct merge is refused by branch protection when the branch is
    protected, and is gated by `merge_ops` when it is not.

    `--admin` overrides branch protection. The scheduler's token is usually the
    repository owner's, so without this omission the second lock would not
    exist: a red pull request is refused for an admin too, until this flag.
    """
    return ["gh", "pr", "merge", str(number), "--squash", "--repo", repo]


def assign_command(assignable_id: str, actor_id: str) -> list[str]:
    # Both ids travel as variables, never spliced into the mutation text.
    return [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={ASSIGN_MUTATION}",
        "-F",
        f"assignableId={assignable_id}",
        "-F",
        f"actorIds[]={actor_id}",
    ]


def edit_labels_command(
    repo: str, number: int, add: Sequence[str], remove: Sequence[str]
) -> list[str]:
    command = ["gh", "issue", "edit", str(number), "--repo", repo]
    for label in add:
        command += ["--add-label", label]
    for label in remove:
        command += ["--remove-label", label]
    return command


def state_command(owner: str, repo: str, *, first: int = 100) -> list[str]:
    return [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={STATE_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"repo={repo}",
        "-F",
        f"first={first}",
    ]


def create_issue_command(repo: str, title: str, labels: Sequence[str]) -> list[str]:
    command = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", "-"]
    for label in labels:
        command += ["--label", label]
    return command


def update_issue_command(
    repo: str, number: int, title: str, labels: Sequence[str], remove_labels: Sequence[str]
) -> list[str]:
    command = [
        "gh",
        "issue",
        "edit",
        str(number),
        "--repo",
        repo,
        "--title",
        title,
        "--body-file",
        "-",
    ]
    for label in labels:
        command += ["--add-label", label]
    for label in remove_labels:
        command += ["--remove-label", label]
    return command


def label_command(repo: str, label: str) -> list[str]:
    # `--force` makes label creation idempotent, which is the whole point here.
    return ["gh", "label", "create", label, "--repo", repo, "--force"]


def item_add_command(project: int, owner: str, issue_url: str) -> list[str]:
    return [
        "gh",
        "project",
        "item-add",
        str(project),
        "--owner",
        owner,
        "--url",
        issue_url,
        "--format",
        "json",
    ]


def item_edit_command(
    *, project_id: str, item_id: str, field_id: str, value: str, option_id: str | None
) -> list[str]:
    command = [
        "gh",
        "project",
        "item-edit",
        "--id",
        item_id,
        "--project-id",
        project_id,
        "--field-id",
        field_id,
    ]
    if option_id is not None:
        return command + ["--single-select-option-id", option_id]
    return command + ["--text", value]


def field_list_command(project: int, owner: str) -> list[str]:
    return [
        "gh",
        "project",
        "field-list",
        str(project),
        "--owner",
        owner,
        # `gh` fetches 30 by default, and truncation is indistinguishable from
        # a missing field: `doctor` would report a field the board has, and
        # `init` would try to create it a second time.
        "--limit",
        "200",
        "--format",
        "json",
    ]


def project_view_command(project: int, owner: str) -> list[str]:
    return ["gh", "project", "view", str(project), "--owner", owner, "--format", "json"]


def field_create_command(project: int, owner: str, spec: FieldSpec) -> list[str]:
    command = [
        "gh",
        "project",
        "field-create",
        str(project),
        "--owner",
        owner,
        "--name",
        spec.name,
        "--data-type",
        spec.data_type,
    ]
    if spec.data_type == SINGLE_SELECT:
        # One argument, comma-joined: options carry spaces and hyphens, and
        # `gh` rejects the flag entirely on a field that is not a single select.
        command += ["--single-select-options", ",".join(spec.options)]
    return command


def field_delete_command(field_id: str) -> list[str]:
    return ["gh", "project", "field-delete", "--id", field_id]


#: Built-in fields refuse `deleteProjectV2Field` ("Only custom fields can be
#: deleted"), which is exactly the case that matters: every board arrives with
#: a `Status` holding Todo/In Progress/Done. Updating the options works on
#: built-in and custom fields alike, and keeps the id the board's views use.
FIELD_UPDATE_MUTATION = """
mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
  updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
    projectV2Field {
      ... on ProjectV2SingleSelectField { id name options { name } }
    }
  }
}
"""


def field_update_command() -> list[str]:
    """The argv. The variables travel in the body, because `-F` cannot carry
    a list of objects."""
    return ["gh", "api", "graphql", "--input", "-"]


def field_update_body(field_id: str, spec: FieldSpec) -> str:
    """The request body, built as data and serialised once.

    Replacing the options drops any value an item held under an option that
    goes away, which is why `init` only ever plans this for an empty board.
    """
    if spec.data_type != SINGLE_SELECT:
        raise ValueError(f"{spec.name} is not a single select, so it has no options to set")
    body = {
        "query": FIELD_UPDATE_MUTATION,
        "variables": {
            "field": field_id,
            # `color` and `description` are not optional on the input type.
            "options": [
                {"name": option, "color": "GRAY", "description": ""} for option in spec.options
            ],
        },
    }
    return json.dumps(body)


def label_list_command(repo: str) -> list[str]:
    return ["gh", "label", "list", "--repo", repo, "--json", "name", "--limit", "200"]


def auth_status_command() -> list[str]:
    return ["gh", "auth", "status"]


def variable_list_command(repo: str) -> list[str]:
    return ["gh", "variable", "list", "--repo", repo, "--json", "name"]


def secret_list_command(repo: str) -> list[str]:
    """Secret *names* only — a secret's value is not readable, by design."""
    return ["gh", "secret", "list", "--repo", repo, "--json", "name"]


def parse_names(payload: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(str(entry["name"]) for entry in payload)


def parse_item_count(payload: dict[str, Any]) -> int:
    """How many items the board holds; unknown reads as populated.

    The count is what licenses deleting and recreating a mis-optioned field,
    so an absent count must never read as "empty and therefore safe".
    """
    items = payload.get("items")
    if isinstance(items, dict) and isinstance(items.get("totalCount"), int):
        return int(items["totalCount"])
    return 1


@dataclass
class GhCli:
    """Production adapter. Unverified until D5 runs it against a scratch repo."""

    repo: str  # "owner/name"
    project: int
    _catalog: FieldCatalog | None = field(default=None, init=False)
    _project_id: str | None = field(default=None, init=False)
    _actor: str | None = field(default=None, init=False)

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    def fetch_state(self, *, plan: str) -> RepoState:
        owner, name = self.repo.split("/", 1)
        return parse_state(json.loads(_run(state_command(owner, name))))

    def ensure_labels(self, labels: Sequence[str]) -> None:
        for label in labels:
            _run(label_command(self.repo, label))

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> int:
        url = _run(create_issue_command(self.repo, title, labels), stdin=body).strip()
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def update_issue(
        self,
        *,
        number: int,
        title: str,
        body: str,
        labels: Sequence[str],
        remove_labels: Sequence[str] = (),
    ) -> None:
        _run(
            update_issue_command(self.repo, number, title, labels, remove_labels),
            stdin=body,
        )

    def add_project_item(self, *, issue_number: int) -> str:
        url = f"https://github.com/{self.repo}/issues/{issue_number}"
        payload = json.loads(_run(item_add_command(self.project, self.owner, url)))
        return str(payload["id"])

    def set_project_field(self, *, item_id: str, field_name: str, value: str) -> None:
        field_id, option_id = self._field_catalog().resolve(field_name, value)
        _run(
            item_edit_command(
                project_id=self._project_node_id(),
                item_id=item_id,
                field_id=field_id,
                value=value,
                option_id=option_id,
            )
        )

    def assign_agent(self, *, number: int, node_id: str) -> None:
        actor = self._agent_actor()
        if actor is None:
            raise RuntimeError(
                f"{self.repo} has no `{AGENT_LOGIN}` among its assignable actors; "
                "enable the coding agent, or route these tasks to the local lane"
            )
        _run(assign_command(node_id, actor))

    def mark_ready(self, *, number: int) -> None:
        _run(ready_command(number, self.repo))

    def merge_pr(self, *, number: int) -> None:
        _run(merge_command(number, self.repo))

    def edit_labels(
        self, *, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        if not add and not remove:
            return
        _run(edit_labels_command(self.repo, number, add, remove))

    # --- the board and diagnostics ports (D5.5) ----------------------------

    def fetch_board(self) -> BoardSnapshot:
        """The project's fields and item count, plus the repository's labels."""
        fields = json.loads(_run(field_list_command(self.project, self.owner)))
        view = json.loads(_run(project_view_command(self.project, self.owner)))
        labels = parse_labels(json.loads(_run(label_list_command(self.repo))))
        return parse_project(fields, items=parse_item_count(view), labels=labels)

    def create_field(self, spec: FieldSpec) -> None:
        _run(field_create_command(self.project, self.owner, spec))
        self._catalog = None  # the cached ids are now a pass out of date

    def delete_field(self, *, field_id: str) -> None:
        _run(field_delete_command(field_id))
        self._catalog = None

    def set_field_options(self, *, field_id: str, spec: FieldSpec) -> None:
        _run(field_update_command(), stdin=field_update_body(field_id, spec))
        self._catalog = None

    def token_scopes(self) -> tuple[str, ...] | None:
        """`None` when the token does not report them, as a workflow token does not."""
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell
            auth_status_command(),
            capture_output=True,
            text=True,
            check=False,
        )
        # `gh auth status` prints to stderr and exits non-zero when logged out,
        # which is a diagnosis rather than a crash.
        return parse_token_scopes(completed.stdout + completed.stderr)

    def agent_available(self) -> bool:
        return self._agent_actor() is not None

    def branch_protected(self, branch: str = "main") -> bool:
        """Does the default branch require a status check?

        A repository with no protection answers 404, which `_run` raises on.
        That is the answer, not a failure: the check exists precisely to report
        it, so it must not take `doctor` down with it.
        """
        try:
            payload = json.loads(_run(protection_command(self.repo, branch)))
        except RuntimeError:
            return False
        return parse_protection(payload)

    def workflow_inputs(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The Actions variable and secret names set on the repository."""
        variables = parse_names(json.loads(_run(variable_list_command(self.repo))))
        secrets = parse_names(json.loads(_run(secret_list_command(self.repo))))
        return variables, secrets

    def _agent_actor(self) -> str | None:
        if self._actor is None:
            owner, name = self.repo.split("/", 1)
            self._actor = parse_agent_actor(json.loads(_run(actor_command(owner, name))))
        return self._actor

    def _field_catalog(self) -> FieldCatalog:
        if self._catalog is None:
            payload = json.loads(_run(field_list_command(self.project, self.owner)))
            self._catalog = parse_field_catalog(payload)
        return self._catalog

    def _project_node_id(self) -> str:
        if self._project_id is None:
            payload = json.loads(_run(project_view_command(self.project, self.owner)))
            self._project_id = str(payload["id"])
        return self._project_id


def _run(command: Sequence[str], *, stdin: str | None = None) -> str:
    completed = subprocess.run(  # noqa: S603 - argv list, never a shell
        list(command),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{command[0]} {command[1]} failed: {completed.stderr.strip()}")
    return completed.stdout
