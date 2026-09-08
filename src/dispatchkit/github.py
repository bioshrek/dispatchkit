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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from dispatchkit.model import PullRequest, TaskId

# Project (v2) fields `apply` owns. `Status` and `Attempts` are excluded on
# purpose: they are derived scheduling state, recomputed on every pass.
FIELD_TASK_ID = "Task ID"
FIELD_LANE = "Lane"
FIELD_VERIFY = "Verify"
APPLIED_FIELDS = (FIELD_TASK_ID, FIELD_LANE, FIELD_VERIFY)

MANAGED_LABEL_PREFIXES = ("plan:", "lane:", "verify:")
DISPATCHKIT_LABEL = "dispatchkit"


@dataclass(frozen=True, slots=True)
class IssueState:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    closed: bool
    project_item_id: str | None
    fields: Mapping[str, str]
    # The scheduler's inputs (D4): assignment is the dispatch lock, and an open
    # linked PR is how "work is under way" is observed without a side table.
    assignees: tuple[str, ...] = ()
    open_prs: tuple[PullRequest, ...] = ()
    # The GraphQL node id (D5). Assigning a bot actor is a mutation over node
    # ids, and the state query already returns it, so dispatch needs no extra
    # round trip.
    node_id: str | None = None


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


@dataclass(frozen=True, slots=True)
class AddProjectItem:
    task_id: TaskId
    number: int | None  # None until the issue it belongs to has been created


@dataclass(frozen=True, slots=True)
class SetProjectField:
    task_id: TaskId
    field_name: str
    value: str


Operation = CreateIssue | UpdateIssue | AddProjectItem | SetProjectField


@dataclass(frozen=True, slots=True)
class AssignAgent:
    """Hand a task to the cloud coding agent (D5).

    Assignment is the dispatch lock, so this is the one operation whose
    ordering matters: it must land before anything that records the fact.
    """

    task_id: TaskId
    number: int
    node_id: str


@dataclass(frozen=True, slots=True)
class LabelIssue:
    """Claim a task for the local lane, or clear a claim."""

    task_id: TaskId
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

    task_id: TaskId
    number: int


#: What a scheduler pass may do. Deliberately narrower than `Operation`: a pass
#: never creates, edits or closes an issue — `apply` owns the graph's shape and
#: only a merged PR closes work. `MarkReady` is the one write that lands on a
#: pull request rather than an issue, and it changes no content.
DispatchOperation = AssignAgent | LabelIssue | MarkReady | SetProjectField


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
    #: Board item id per task, for items the board *already* held. Without it
    #: a field change on an issue added by an earlier run has nowhere to be
    #: written: the executor only learns ids from the items it adds itself.
    item_ids: Mapping[TaskId, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.operations)


@dataclass(frozen=True, slots=True)
class ApplyResult:
    created: int
    updated: int
    project_items: int
    fields_set: int
    issue_numbers: tuple[int, ...]


class GitHubApi(Protocol):
    """The port `apply` needs. Adapters live in `gh_cli.py`."""

    def fetch_state(self, *, plan: str) -> RepoState: ...

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

    def add_project_item(self, *, issue_number: int) -> str: ...

    def set_project_field(self, *, item_id: str, field_name: str, value: str) -> None: ...

    def assign_agent(self, *, number: int, node_id: str) -> None: ...

    def mark_ready(self, *, number: int) -> None: ...

    def edit_labels(
        self, *, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None: ...
