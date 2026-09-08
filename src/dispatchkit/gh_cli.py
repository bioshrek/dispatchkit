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

from dispatchkit.github import IssueState, RepoState

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
              source { ... on PullRequest { number state } }
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
        assignees=tuple(
            user["login"] for user in (node.get("assignees", {}).get("nodes") or [])
        ),
        open_prs=_parse_open_prs(node),
        node_id=node.get("id"),
    )


def _parse_open_prs(node: dict[str, Any]) -> tuple[int, ...]:
    """Open pull requests cross-referencing this issue, in the order recorded."""
    numbers: list[int] = []
    for event in node.get("timelineItems", {}).get("nodes") or []:
        source = event.get("source") or {}
        if source.get("state") == "OPEN" and "number" in source:
            numbers.append(int(source["number"]))
    return tuple(numbers)


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


@dataclass(frozen=True, slots=True)
class FieldCatalog:
    """Project (v2) field ids, and option ids for single-select fields."""

    ids: dict[str, str]
    options: dict[str, dict[str, str]]

    def option_id(self, field_name: str, value: str) -> str | None:
        return self.options.get(field_name, {}).get(value)


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
        catalog = self._field_catalog()
        field_id = catalog.ids[field_name]
        _run(
            item_edit_command(
                project_id=self._project_node_id(),
                item_id=item_id,
                field_id=field_id,
                value=value,
                option_id=catalog.option_id(field_name, value),
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

    def edit_labels(
        self, *, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        if not add and not remove:
            return
        _run(edit_labels_command(self.repo, number, add, remove))

    def _agent_actor(self) -> str | None:
        if self._actor is None:
            owner, name = self.repo.split("/", 1)
            self._actor = parse_agent_actor(json.loads(_run(actor_command(owner, name))))
        return self._actor

    def _field_catalog(self) -> FieldCatalog:
        if self._catalog is None:
            payload = json.loads(
                _run(
                    [
                        "gh",
                        "project",
                        "field-list",
                        str(self.project),
                        "--owner",
                        self.owner,
                        "--format",
                        "json",
                    ]
                )
            )
            self._catalog = parse_field_catalog(payload)
        return self._catalog

    def _project_node_id(self) -> str:
        if self._project_id is None:
            payload = json.loads(
                _run(
                    [
                        "gh",
                        "project",
                        "view",
                        str(self.project),
                        "--owner",
                        self.owner,
                        "--format",
                        "json",
                    ]
                )
            )
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
