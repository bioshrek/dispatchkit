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

from dispatchkit.model import (
    DEFAULT_BASE,
    Base,
    Lane,
    MergedPr,
    PullRequest,
    TaskId,
    TaskRef,
    Verify,
)

MANAGED_LABEL_PREFIXES = ("plan:", "lane:", "verify:")
DISPATCHKIT_LABEL = "dispatchkit"

#: The human's "not now" (D13.1c). It lives here rather than with the resolver's
#: other labels because the adapter reads it back off the issue timeline, and a
#: label spelt twice is a wire format spelt twice.
LABEL_HOLD = "dispatch:hold"

#: The local lane's claim. It lives here rather than with the resolver's other
#: labels because the adapter reads it off the timeline: a local dispatch is a
#: mark, not an assignment, so the mark is the event `attempts` derives from.
LABEL_LOCAL_CLAIM = "dispatch:local"

#: The retry budget's terminal state (D7). Moved down here from `resolve` in
#: D6.6: every other `dispatch:` label already lived in this module, and having
#: one of them somewhere else is how all three came to be left out of
#: `REQUIRED_LABELS`. `resolve` imports it from here now.
LABEL_STUCK = "dispatch:stuck"

#: Labels `init` has to create on the repository itself, in two families.
#:
#: The ones the pipeline **filters on**: `dispatchkit` is the important one,
#: because the state query selects by it, so a repository without it returns
#: nothing and a pass is a silent no-op. The lane and verify labels mirror the
#: machine block, which is what makes a saved issue-list URL a live view of a
#: plan.
#:
#: And the ones the pipeline **writes** — added in D6.6, after the local lane's
#: first live run died marking an issue with a `dispatch:local` that no
#: repository had. A label the scheduler applies is as much a precondition as a
#: label it reads: `gh issue edit --add-label` fails on a name that does not
#: exist, so the miss is not a degraded pass, it is no pass at all. The cloud
#: lane never found this because a cloud dispatch is an assignment rather than
#: a label, and no live task had yet exhausted its retry budget.
#:
#: `dispatch:hold` is created even though only a human writes it: a label the
#: repository does not offer cannot be picked from the issue UI, and picking it
#: is the whole of that feature.
REQUIRED_LABELS: tuple[str, ...] = (
    DISPATCHKIT_LABEL,
    *(f"lane:{lane.value}" for lane in Lane),
    *(f"verify:{verify.value}" for verify in Verify),
    LABEL_LOCAL_CLAIM,
    LABEL_STUCK,
    LABEL_HOLD,
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
    # Pull requests linked to this issue that have already landed, and the
    # branch each landed on (D16). Off the default branch GitHub ignores a
    # closing keyword entirely, so this is the only evidence that the work is
    # in -- and the branch is half of it.
    merged: tuple[MergedPr, ...] = ()
    # When the issue closed (D15). The end of `work`, and the only one of the
    # retrospective's timestamps the issue itself holds. `None` on an open
    # issue and on any payload recorded before the field was asked for, which
    # is an absence rather than an error.
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RepoState:
    issues: tuple[IssueState, ...]
    #: The head branch of every open pull request in the repository (D16).
    #: A plan's own pull request is linked to no issue -- it proposes the whole
    #: branch -- so it cannot be found the way task pull requests are, and
    #: "have I opened this already" is the only question that keeps opening it
    #: idempotent.
    open_pr_heads: tuple[str, ...] = ()


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
    #: The branch the agent starts from and targets (D16). Always set — the
    #: adapter should never have to decide what an absent base means.
    base: Base = DEFAULT_BASE


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


@dataclass(frozen=True, slots=True)
class CloseIssue:
    """Close a task whose pull request has landed on its plan branch (D16).

    GitHub honours a closing keyword only on the default branch — for anything
    else its documentation says the keywords "are ignored, no links are
    created". A plan branch is not the default branch, so without this every
    plan on one deadlocks on its first task: the work merges, the issue stays
    open, and nothing that depends on it ever becomes ready.

    A pass still never *decides* anything about the graph's shape. This records
    a fact the repository already contains rather than making one, which is why
    it can sit beside operations that only ever hand work out.
    """

    ref: TaskRef
    number: int


@dataclass(frozen=True, slots=True)
class OpenPlanPr:
    """Propose a finished plan branch to the branch it came from (D16).

    Opened when the plan's last task closes, and never merged by dispatchkit --
    not even when every task in it said `verify: auto`. That declaration is
    about a task: a reviewed unit of a reviewed graph. A plan is the sum, and
    the sum is the thing no one signed off. Collecting the work on a branch is
    what buys a human one reading of the whole before it reaches the default
    branch, and merging it here would spend that.

    There is deliberately no `verify` on this operation. A field is a thing a
    later change can read.
    """

    base: Base
    plan: str
    #: What the plan branch is proposed *to*: the repository's trunk, which is
    #: `DEFAULT_BASE` unless a plan branch is itself stacked on another.
    onto: Base = DEFAULT_BASE
    #: The task issues this plan shipped, for the body.
    issues: tuple[int, ...] = ()


#: What a scheduler pass may do. Deliberately narrower than `Operation`: a pass
#: never creates or edits an issue — `apply` owns the graph's shape. It may
#: close one, but only to record a merge that has already happened (D16). Every
#: member acts on the repository itself, because since D14 there is nowhere
#: else to write: status is derived and printed, never stored.
DispatchOperation = (
    AssignAgent | UnassignAgent | LabelIssue | MarkReady | MergePr | CloseIssue | OpenPlanPr
)


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
    #: The branch this plan integrates on (D16). Carried on the plan rather
    #: than passed beside it because execution is handed the plan and nothing
    #: else, and the branch has to exist before any task can be cut from it.
    base: Base = DEFAULT_BASE

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

    def ensure_branch(self, *, base: Base) -> None:
        """Make the plan branch if it is not already there (D16).

        `ensure`, like labels, rather than a diffed operation: the API answers
        this idempotently, so teaching `RepoState` about refs would buy nothing
        but a way for the operation list -- which is what the convergence test
        reads -- to be non-empty for ever.
        """

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

    def assign_agent(self, *, number: int, node_id: str, base: Base) -> None: ...

    def unassign_agent(self, *, number: int, assignees: Sequence[str]) -> None: ...

    def mark_ready(self, *, number: int) -> None: ...

    def merge_pr(self, *, number: int) -> None: ...

    def close_issue(self, *, number: int) -> None:
        """Because GitHub will not, off the default branch (D16)."""

    # The local lane (D6). The cloud agent opens its own pull request and
    # comments for itself; here the executor is the agent's hands.
    def open_pr(self, *, head: str, title: str, body: str, base: Base) -> int:
        """Against the branch the plan integrates on (D16). Required for the
        same reason as `create_worktree`: an omitted base is `main`, and a
        pull request opened against `main` succeeds."""

    def comment(self, *, number: int, body: str) -> None: ...

    def edit_labels(
        self, *, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None: ...
