"""GitHub-facing types for `apply` and the scheduler (D3).

Deliberately split three ways so only the last part touches the network:

- `IssueState` / `RepoState` — a snapshot of what GitHub currently holds.
- the `Operation` union — what `apply` decided to do about it, as data, so the
  plan can be printed, diffed and asserted on without a client at all.
- `GitHubApi` — the port an adapter implements (`gh_cli.GhCli` in production,
  an in-memory double in tests).

GitHub is the only state store, so `RepoState` is the whole world: there is no
local database that could disagree with it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from dispatchkit.model import Lane, PullRequest, TaskId, TaskRef, Verify

MANAGED_LABEL_PREFIXES = ("plan:", "lane:", "verify:")
DISPATCHKIT_LABEL = "dispatchkit"

#: The human's "not now" (D13.1c). It lives here rather than with the resolver's
#: other labels because the adapter reads it back off the issue timeline, and a
#: label spelt twice is a wire format spelt twice.
LABEL_HOLD = "dispatch:hold"

#: Labels the pipeline filters on, and the only thing `init` has to create on
#: the repository itself. `dispatchkit` is the important one: the state query
#: selects by it, so a repository without it returns nothing and a pass is a
#: silent no-op. The lane and verify labels mirror the machine block, which is
#: what makes a saved issue-list URL a live view of a plan.
REQUIRED_LABELS: tuple[str, ...] = (
    DISPATCHKIT_LABEL,
    *(f"lane:{lane.value}" for lane in Lane),
    *(f"verify:{verify.value}" for verify in Verify),
)


def missing_labels(labels: Sequence[str]) -> tuple[str, ...]:
    return tuple(label for label in REQUIRED_LABELS if label not in labels)


@dataclass(frozen=True, slots=True)
class IssueState:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    closed: bool
    # Closed as *not planned* rather than as completed (D13.1). A separate
    # field rather than a third state, because everything that asks "is this
    # finished with?" still wants `closed`; only dependency satisfaction cares
    # which kind of closed it was.
    cancelled: bool = False
    # The scheduler's inputs (D4): assignment is the dispatch lock, and an open
    # linked PR is how "work is under way" is observed without a side table.
    assignees: tuple[str, ...] = ()
    open_prs: tuple[PullRequest, ...] = ()
    # The GraphQL node id (D5). Assigning a bot actor is a mutation over node
    # ids, and the state query already returns it, so dispatch needs no extra
    # round trip.
    node_id: str | None = None
    # When the agent was assigned, once per dispatch (D7). Read from the issue
    # timeline rather than anywhere of our own, so the count of attempts stays
    # derived from the repository like every other part of `Status`.
    dispatches: tuple[datetime, ...] = ()
    # When `dispatch:hold` was applied, from the same timeline (D13.1c). A hold
    # ends a dispatch without the agent having failed, so the retry budget has
    # to be able to tell the two apart, and the repository is the only place
    # that remembers which.
    holds: tuple[datetime, ...] = ()


@dataclass(frozen=True, slots=True)
class RepoState:
    issues: tuple[IssueState, ...]


@dataclass(frozen=True, slots=True)
class CreateIssue:
    task_id: TaskId
    title: str
    body: str
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpdateIssue:
    task_id: TaskId
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    # Labels `apply` owns that are no longer wanted. Carried in the operation
    # so the adapter needs no extra read to work out what to strip.
    remove_labels: tuple[str, ...] = ()


Operation = CreateIssue | UpdateIssue


@dataclass(frozen=True, slots=True)
class AssignAgent:
    """Hand a task to the cloud coding agent (D5).

    Assignment is the dispatch lock, so this is the one operation whose
    ordering matters: it must land before anything that records the fact.
    """

    ref: TaskRef
    number: int
    node_id: str


@dataclass(frozen=True, slots=True)
class UnassignAgent:
    """Take a stalled task back from the agent (D7).

    Assignment is the dispatch lock, so releasing it *is* the retry: the task
    rejoins the ready set on the next pass with no other bookkeeping. The
    assignees are named rather than cleared wholesale, so a human who assigned
    themselves to watch a task keeps their assignment.
    """

    ref: TaskRef
    number: int
    assignees: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LabelIssue:
    """Claim a task for the local lane, or clear a claim."""

    ref: TaskRef
    number: int
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MarkReady:
    """Take a `verify: auto` pull request out of draft (D5.7).

    Copilot leaves its pull requests as drafts when it finishes, and a draft
    cannot be merged. `verify: auto` is a declaration that no human judgment is
    required, so the draft gate is one dispatchkit has already been told it may
    clear. It does not touch the tree, and it is only ever emitted over a run
    that has actually passed.
    """

    ref: TaskRef
    number: int


@dataclass(frozen=True, slots=True)
class MergePr:
    """Squash-merge a `verify: auto` pull request (D9).

    The one operation in the system that mutates `main` with no human in the
    loop, so it is the one with the most gates in front of it. It is a *direct*
    merge, never `--auto`: the probe behind D9 found that `gh pr merge --auto`
    silently degrades to an immediate merge on a repository without branch
    protection, which would merge unreviewed code having consulted nothing.
    """

    ref: TaskRef
    number: int


#: What a scheduler pass may do. Deliberately narrower than `Operation`: a pass
#: never creates, edits or closes an issue — `apply` owns the graph's shape and
#: only a merged PR closes work. Every member acts on the repository itself,
#: because since D14 there is nowhere else to write: status is derived and
#: printed, never stored.
DispatchOperation = AssignAgent | UnassignAgent | LabelIssue | MarkReady | MergePr


@dataclass(frozen=True, slots=True)
class Notice:
    """Something a human should look at, which `apply` will not act on itself."""

    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: [{self.where}] {self.message}"


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    operations: tuple[Operation, ...]
    notices: tuple[Notice, ...]

    def __bool__(self) -> bool:
        return bool(self.operations)


@dataclass(frozen=True, slots=True)
class ApplyResult:
    created: int
    updated: int
    issue_numbers: tuple[int, ...]


class GitHubApi(Protocol):
    """The port `apply` needs. Adapters live in `gh_cli.py`."""

    def fetch_state(self) -> RepoState: ...

    def ensure_labels(self, labels: Sequence[str]) -> None: ...

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> int: ...

    def update_issue(
        self,
        *,
        number: int,
        title: str,
        body: str,
        labels: Sequence[str],
        remove_labels: Sequence[str] = (),
    ) -> None: ...

    def assign_agent(self, *, number: int, node_id: str) -> None: ...

    def unassign_agent(self, *, number: int, assignees: Sequence[str]) -> None: ...

    def mark_ready(self, *, number: int) -> None: ...

    def merge_pr(self, *, number: int) -> None: ...

    def edit_labels(
        self, *, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None: ...
