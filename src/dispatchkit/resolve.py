"""D4: the readiness resolver — the scheduler's brain, as a pure function.

Two claims are being made here, and both are the reason this module contains no
I/O at all:

1. **Status is derived, never stored.** `Status` on the Project board is a
   projection of the issues, recomputed from scratch on every pass. Delete the
   board and it rebuilds; disagree with it and the next pass overwrites it.
   There is no state machine to get wedged, because there is no state.
2. **Assignment is the lock.** A task is ready only while it is unassigned, so
   dispatching it (assigning it) removes it from the ready set. Two schedulers
   racing on the same repo therefore converge instead of double-dispatching,
   without a lease, a lockfile or a mutex — GitHub's own write is the lock.

Readiness is deliberately a *one-step* check ("are my direct dependencies
closed?") rather than a recursive walk. That makes a cycle harmless: nothing in
it ever opens, but the resolver still terminates.

Admission is kept separate from status. A task waiting on spend approval or
sitting at its retry budget is still `Ready` — the work is unblocked, we are
just choosing not to start it. Folding those into `Status` would invent board
values the plan does not define and would lose the distinction between "cannot
run" and "will not run yet".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from dispatchkit.block import MachineBlock, parse_block
from dispatchkit.config import SchedulerConfig
from dispatchkit.errors import GraphError
from dispatchkit.github import (
    DISPATCHKIT_LABEL,
    Notice,
    RepoState,
    SetProjectField,
)
from dispatchkit.model import Checks, Lane, PullRequest, TaskId, Verify

FIELD_STATUS = "Status"
FIELD_ATTEMPTS = "Attempts"

LABEL_STUCK = "dispatch:stuck"
LABEL_SPEND_APPROVED = "spend:approved"
LABEL_LOCAL_CLAIM = "dispatch:local"

_WILDCARD = "*?["


class Status(Enum):
    """The Project board's `Status` column. Values are the board's labels."""

    BLOCKED = "Blocked"
    READY = "Ready"
    DISPATCHED = "Dispatched"
    IN_REVIEW = "In Review"
    AUTO_MERGING = "Auto-merging"
    DONE = "Done"


#: Statuses that occupy a lane slot: work an agent is already doing.
IN_FLIGHT = frozenset({Status.DISPATCHED, Status.IN_REVIEW, Status.AUTO_MERGING})


@dataclass(frozen=True, slots=True)
class TaskItem:
    """One task as the scheduler sees it: machine block plus live issue state."""

    block: MachineBlock
    number: int
    closed: bool
    assignees: tuple[str, ...]
    labels: tuple[str, ...]
    open_prs: tuple[PullRequest, ...]
    attempts: int
    project_item_id: str | None
    fields: Mapping[str, str]
    node_id: str | None = None

    @property
    def id(self) -> TaskId:
        return self.block.id

    @property
    def lane(self) -> Lane:
        return self.block.lane

    @property
    def verify(self) -> Verify:
        return self.block.verify

    @property
    def touches(self) -> tuple[str, ...]:
        return self.block.touches

    @property
    def claimed(self) -> bool:
        """Has an agent taken this task? Cloud assigns; the local daemon labels."""
        return bool(self.assignees) or LABEL_LOCAL_CLAIM in self.labels

    @property
    def checks(self) -> Checks:
        """CI's verdict across every open PR on this task, worst first."""
        return Checks.combine(pr.checks for pr in self.open_prs)


@dataclass(frozen=True, slots=True)
class Deferral:
    task_id: TaskId
    reason: str
    detail: str = ""

    def __str__(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.task_id}: {self.reason}{suffix}"


@dataclass(frozen=True, slots=True)
class AdmissionPlan:
    admitted: tuple[TaskId, ...]
    deferred: tuple[Deferral, ...]


def build_items(state: RepoState, *, plan: str) -> tuple[tuple[TaskItem, ...], tuple[Notice, ...]]:
    """Turn a repo snapshot into resolver input, in issue order."""
    items: list[TaskItem] = []
    notices: list[Notice] = []

    for issue in sorted(state.issues, key=lambda issue: issue.number):
        if DISPATCHKIT_LABEL not in issue.labels:
            continue
        try:
            block = parse_block(issue.body)
        except GraphError as exc:
            notices.append(
                Notice(
                    "unreadable-issue",
                    f"#{issue.number}",
                    f"dispatchkit block is unreadable ({exc.issues[0].code}); "
                    "the scheduler will ignore this issue",
                )
            )
            continue
        if block.plan != plan:
            continue
        items.append(
            TaskItem(
                block=block,
                number=issue.number,
                closed=issue.closed,
                assignees=issue.assignees,
                labels=issue.labels,
                open_prs=issue.open_prs,
                attempts=_attempts(issue.fields),
                project_item_id=issue.project_item_id,
                fields=issue.fields,
                node_id=issue.node_id,
            )
        )
    return tuple(items), tuple(notices)


def resolve(items: Sequence[TaskItem]) -> dict[TaskId, Status]:
    """Derive every task's status from the issues alone."""
    closed = {task.id for task in items if task.closed}
    return {task.id: _status_of(task, closed) for task in items}


def _status_of(task: TaskItem, closed: frozenset[TaskId] | set[TaskId]) -> Status:
    if task.closed:
        return Status.DONE
    if task.open_prs:
        # A PR exists, so the work happened regardless of how it was claimed;
        # `verify` decides who closes it out. `Auto-merging` is a claim that
        # CI is going to decide, so it is only made while CI is in a position
        # to: a run held for approval, or a red one, needs a human, which is
        # exactly what `In Review` means. Saying `Auto-merging` over a pipeline
        # that will never start would be the board asserting something false.
        if task.verify is Verify.AUTO and not task.checks.stalled:
            return Status.AUTO_MERGING
        return Status.IN_REVIEW
    if task.claimed:
        return Status.DISPATCHED
    # A dependency with no issue at all (deleted, or never applied) is not
    # closed, so its dependents stay blocked. Failing safe beats guessing.
    if all(dep in closed for dep in task.block.depends):
        return Status.READY
    return Status.BLOCKED


def ci_notices(items: Sequence[TaskItem]) -> tuple[Notice, ...]:
    """Report `verify: auto` tasks whose CI cannot run without a human.

    Absorbing this into `In Review` alone would be quietly misleading: the
    board would invite someone to review a pull request that cannot merge, and
    the reason would be a checkbox two pages deep in the Actions tab. Only
    `BLOCKED` is reported — a failing run is somebody's bug, not a gate that
    can be clicked away, and suggesting otherwise would waste the reader's
    time.
    """
    notices = []
    for task in items:
        if task.closed or task.verify is not Verify.AUTO:
            continue
        held = [pr.number for pr in task.open_prs if pr.checks is Checks.BLOCKED]
        if not held:
            continue
        listed = ", ".join(f"#{number}" for number in held)
        notices.append(
            Notice(
                "ci-approval-required",
                f"#{task.number}",
                f"{listed}: GitHub is holding the workflow runs for approval, so "
                "`verify: auto` cannot complete. Approve them in the Actions tab, "
                "or re-queue with `gh run rerun <id>` — note that either way you "
                "are choosing to run agent-authored code.",
            )
        )
    return tuple(notices)


def reconcile_ops(
    items: Sequence[TaskItem], statuses: Mapping[TaskId, Status]
) -> tuple[SetProjectField, ...]:
    """Write back only the statuses the board disagrees with."""
    ops = []
    for task in items:
        if task.project_item_id is None:
            continue  # not on the board yet; `apply` adds it
        wanted = statuses[task.id]
        if task.fields.get(FIELD_STATUS) != wanted.value:
            ops.append(SetProjectField(task.id, FIELD_STATUS, wanted.value))
    return tuple(ops)


def admit(
    items: Sequence[TaskItem],
    statuses: Mapping[TaskId, Status],
    config: SchedulerConfig,
) -> AdmissionPlan:
    """Choose which ready tasks to start now, respecting caps, spend and scope.

    Plan order is ascending issue number, which is the order `apply` created
    them, which is the order they appear in the graph file — so the author's
    ordering is the tie-break, and the choice is reproducible.
    """
    in_flight: dict[Lane, int] = {}
    active: list[tuple[TaskId, tuple[str, ...]]] = []
    for task in items:
        if statuses[task.id] in IN_FLIGHT:
            in_flight[task.lane] = in_flight.get(task.lane, 0) + 1
            active.append((task.id, task.touches))

    admitted: list[TaskId] = []
    deferred: list[Deferral] = []

    for task in sorted(items, key=lambda task: task.number):
        if statuses[task.id] is not Status.READY:
            continue
        deferral = _gate(task, config, in_flight, active)
        if deferral is not None:
            deferred.append(deferral)
            continue
        admitted.append(task.id)
        in_flight[task.lane] = in_flight.get(task.lane, 0) + 1
        active.append((task.id, task.touches))

    return AdmissionPlan(tuple(admitted), tuple(deferred))


def _gate(
    task: TaskItem,
    config: SchedulerConfig,
    in_flight: Mapping[Lane, int],
    active: Sequence[tuple[TaskId, tuple[str, ...]]],
) -> Deferral | None:
    if LABEL_STUCK in task.labels:
        return Deferral(task.id, "stuck", "labelled dispatch:stuck")
    if task.attempts >= config.retry_budget:
        return Deferral(task.id, "stuck", f"{task.attempts} attempts, budget {config.retry_budget}")
    if task.block.spend and LABEL_SPEND_APPROVED not in task.labels:
        return Deferral(task.id, "awaiting-spend-approval", "add `spend:approved` to release")
    cap = config.cap(task.lane)
    if in_flight.get(task.lane, 0) >= cap:
        return Deferral(task.id, "lane-cap", f"{task.lane.value} cap {cap}")
    blocker = _scope_conflict(task, active)
    if blocker is not None:
        return Deferral(task.id, "file-scope-conflict", f"overlaps {blocker}")
    return None


def _scope_conflict(
    task: TaskItem, active: Iterable[tuple[TaskId, tuple[str, ...]]]
) -> TaskId | None:
    for other_id, other_touches in active:
        if any(
            _overlaps(mine, theirs) for mine in task.touches for theirs in other_touches
        ):
            return other_id
    return None


def _overlaps(left: str, right: str) -> bool:
    """Would two `touches` patterns plausibly hit the same file?

    Compared on literal prefixes only — no globbing, no filesystem. The result
    is conservative in one direction on purpose: it may serialise two tasks
    that would in fact have been fine, but it will not let two agents edit the
    same tree at once. An unnecessary wait costs minutes; a bad merge costs a
    human's afternoon.
    """
    left_dir, left_wild = _prefix(left)
    right_dir, right_wild = _prefix(right)
    if not (left_wild or right_wild):
        return left == right
    return left_dir.startswith(right_dir) or right_dir.startswith(left_dir)


def _prefix(pattern: str) -> tuple[str, bool]:
    """The pattern's fixed directory prefix, and whether it globs at all."""
    cut = min((pattern.find(char) for char in _WILDCARD if char in pattern), default=-1)
    if cut < 0:
        head, _, _ = pattern.rpartition("/")
        return f"{head}/" if head else "", False
    head = pattern[:cut]
    head, _, _ = head.rpartition("/")
    return f"{head}/" if head else "", True


def _attempts(fields: Mapping[str, str]) -> int:
    try:
        return int(fields.get(FIELD_ATTEMPTS, "0"))
    except ValueError:
        return 0
