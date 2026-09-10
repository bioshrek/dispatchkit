"""`gh`-CLI adapter for the `GitHubApi` port (D3).

Split into pure parts and a thin subprocess shim so most of it is testable
offline: `parse_state` turns a recorded API payload into value objects, the
`*_command` helpers build argv lists, and `_run` is the only function that
actually shells out.

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
from datetime import datetime
from typing import Any

from dispatchkit.doctor import parse_labels, parse_token_scopes
from dispatchkit.github import LABEL_HOLD, IssueState, RepoState
from dispatchkit.model import Checks, PullRequest

# The coding agent is a bot actor, so it cannot be assigned with
# `gh issue edit --add-assignee`: it has to be looked up by capability and
# handed over through a mutation.
AGENT_LOGIN = "copilot-swe-agent"
#: Every login the cloud agent appears under in a timeline.
AGENT_LOGINS = frozenset({AGENT_LOGIN, "Copilot"})

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
        stateReason
        labels(first: 20) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        holds: timelineItems(first: 50, itemTypes: [LABELED_EVENT]) {
          nodes {
            ... on LabeledEvent {
              createdAt
              label { name }
            }
          }
        }
        dispatches: timelineItems(first: 50, itemTypes: [ASSIGNED_EVENT]) {
          nodes {
            ... on AssignedEvent {
              createdAt
              assignee { ... on Bot { login } ... on User { login } }
            }
          }
        }
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
    return IssueState(
        number=int(node["number"]),
        title=node["title"],
        body=node["body"] or "",
        labels=tuple(label["name"] for label in node["labels"]["nodes"]),
        closed=node["state"] == "CLOSED",
        # Absent in a recorded payload from before the field was requested, and
        # `REOPENED` on an open issue. Both mean "not cancelled": guessing
        # otherwise would strand tasks that are merely done.
        cancelled=node["state"] == "CLOSED" and node.get("stateReason") == "NOT_PLANNED",
        assignees=tuple(user["login"] for user in (node.get("assignees", {}).get("nodes") or [])),
        open_prs=_parse_open_prs(node),
        node_id=node.get("id"),
        dispatches=_parse_dispatches(node),
        holds=_parse_holds(node),
    )


def _parse_holds(node: dict[str, Any]) -> tuple[datetime, ...]:
    """When a human said "not now", so an attempt can be told from a failure."""
    stamps = [
        _stamp(event.get("createdAt"))
        for event in node.get("holds", {}).get("nodes") or []
        if (event.get("label") or {}).get("name") == LABEL_HOLD
    ]
    return tuple(sorted(stamp for stamp in stamps if stamp is not None))


def _stamp(created: str | None) -> datetime | None:
    return datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None


def _parse_dispatches(node: dict[str, Any]) -> tuple[datetime, ...]:
    """When the agent was assigned, once per dispatch.

    Only the agent's own assignment counts. GitHub records a second
    AssignedEvent for the human who triggered the dispatch -- confirmed live on
    the sandbox -- so counting the events wholesale would score every attempt
    twice against the retry budget.
    """
    stamps: list[datetime] = []
    for event in node.get("dispatches", {}).get("nodes") or []:
        assignee = event.get("assignee") or {}
        if assignee.get("login") not in AGENT_LOGINS:
            continue
        created = event.get("createdAt")
        if created:
            stamps.append(datetime.fromisoformat(created.replace("Z", "+00:00")))
    return tuple(sorted(stamps))


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


def unassign_command(number: int, repo: str, assignees: Sequence[str]) -> list[str]:
    """Release a stalled dispatch. Named assignees only, never `--remove-assignee` on all."""
    argv = ["gh", "issue", "edit", str(number), "--repo", repo]
    for login in assignees:
        argv += ["--remove-assignee", login]
    return argv


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


def label_list_command(repo: str) -> list[str]:
    return ["gh", "label", "list", "--repo", repo, "--json", "name", "--limit", "200"]


def auth_status_command() -> list[str]:
    return ["gh", "auth", "status"]


@dataclass
class GhCli:
    """Production adapter. One repository, one `gh` credential, no board."""

    repo: str  # "owner/name"
    _actor: str | None = field(default=None, init=False)

    def fetch_state(self) -> RepoState:
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

    def assign_agent(self, *, number: int, node_id: str) -> None:
        actor = self._agent_actor()
        if actor is None:
            raise RuntimeError(
                f"{self.repo} has no `{AGENT_LOGIN}` among its assignable actors; "
                "enable the coding agent, or route these tasks to the local lane"
            )
        _run(assign_command(node_id, actor))

    def unassign_agent(self, *, number: int, assignees: Sequence[str]) -> None:
        _run(unassign_command(number, self.repo, assignees))

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

    # --- the diagnostics port (D5.5) ---------------------------------------

    def fetch_labels(self) -> tuple[str, ...]:
        """Every label the repository defines, for `doctor` and `init`."""
        return parse_labels(json.loads(_run(label_list_command(self.repo))))

    def token_scopes(self) -> tuple[str, ...] | None:
        """`None` when the token does not report them, which now means logged out."""
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

    def _agent_actor(self) -> str | None:
        if self._actor is None:
            owner, name = self.repo.split("/", 1)
            self._actor = parse_agent_actor(json.loads(_run(actor_command(owner, name))))
        return self._actor


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
