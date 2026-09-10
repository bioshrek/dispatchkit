"""D5: one scheduler pass — load, resolve, report, admit, dispatch.

Every plan in the repository, not one (D13). Readiness is still resolved plan
by plan — a `depends` edge never crosses a graph file — but the admitted set is
pooled and the caps are counted once, because what they bound was never a plan:
`caps.cloud` is review capacity and `caps.local` is a workstation. Three active
plans under a per-plan cap meant three times the open pull requests a person
agreed to read. Priority falls out as first-in-first-out by issue number, which
is repo-global and monotonic — oldest plan first, task order within it, finish
what you started.

`plan_tick` is pure: snapshot in, operations out. `execute_tick` is the only
thing that mutates, and the only mutation that matters is assignment, because
**assignment is the lock**. A task is ready only while unassigned, so the act
of dispatching removes it from the ready set. Two passes racing on the same
repository converge instead of double-dispatching, without a lease or a
lockfile — and a `replaceActorsForAssignable` that loses the race is a no-op
rather than a second assignee.

Nothing here is recorded anywhere afterwards (D14). The pass derives every
status, prints them, and hands out what the caps allow; dying part-way through
costs at most the operations it had not reached yet, because there is no second
artifact that could be left disagreeing with the issues.

A pass never creates, edits, or closes an issue. `apply` owns the graph's
shape, and only a merged PR closes work; the scheduler only ever hands work
out and says what it sees.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import (
    AssignAgent,
    DispatchOperation,
    GitHubApi,
    LabelIssue,
    MarkReady,
    MergePr,
    Notice,
    RepoState,
    UnassignAgent,
)
from dispatchkit.model import Lane, TaskRef
from dispatchkit.resolve import (
    LABEL_LOCAL_CLAIM,
    Deferral,
    Status,
    TaskItem,
    admit,
    build_items,
    ci_notices,
    merge_ops,
    ready_ops,
    resolve,
    stall_ops,
)


@dataclass(frozen=True, slots=True)
class TickPlan:
    operations: tuple[DispatchOperation, ...]
    #: What this pass will have made true by the time it ends, for the report.
    #: Held for rendering only — every decision above is taken from the state
    #: as read, and the next pass derives all of this again from scratch.
    statuses: Mapping[TaskRef, Status]
    admitted: tuple[TaskRef, ...]
    deferred: tuple[Deferral, ...]
    notices: tuple[Notice, ...]

    def __bool__(self) -> bool:
        return bool(self.operations)


@dataclass(frozen=True, slots=True)
class TickResult:
    dispatched: int
    #: Pull requests taken out of draft. Kept apart from `dispatched` because
    #: no work was handed to an agent: an existing PR was merely un-drafted.
    readied: int = 0
    #: Pull requests squash-merged. The only count here that changed `main`.
    merged: int = 0
    #: Merges GitHub refused. A conflict, a protection rule or a race are all
    #: ordinary outcomes, so they are reported rather than raised, and the pass
    #: carries on with the tasks behind them.
    refused: tuple[Notice, ...] = ()
    #: Dispatches reclaimed after producing no pull request (D7).
    reclaimed: int = 0


def plan_tick(state: RepoState, *, config: SchedulerConfig, now: datetime) -> TickPlan:
    items, notices = build_items(state)
    statuses = resolve(items)
    admission = admit(items, statuses, config)

    by_ref = {task.ref: task for task in items}
    dispatch_ops: list[DispatchOperation] = []
    dispatched: list[TaskRef] = []
    blocked_notices: list[Notice] = []

    for ref in admission.admitted:
        operation = _dispatch_op(by_ref[ref])
        if isinstance(operation, Notice):
            blocked_notices.append(operation)
            continue
        dispatch_ops.append(operation)
        dispatched.append(ref)

    # The report must not say `Ready` for an issue this pass just assigned, so
    # the statuses are advanced for exactly the tasks that were handed out. The
    # next pass derives the same value from the assignee, so the printed line
    # and the repository agree.
    projected: dict[TaskRef, Status] = {**statuses}
    projected.update(dict.fromkeys(dispatched, Status.DISPATCHED))

    # A reclaimed task is unassigned by the time this pass ends, so the report
    # says what will be true rather than what was: `Ready` for one going back
    # into the queue, `Stuck` for one that has spent its budget. Both are what
    # the next pass derives unaided.
    stalls = stall_ops(items, config, now)
    for stall in stalls:
        match stall:
            case UnassignAgent():
                projected[stall.ref] = Status.READY
            case LabelIssue():
                projected[stall.ref] = Status.STUCK

    # Planned from the state as read. A draft cleared by this pass becomes
    # `Auto-merging` on the next one, which is the same convergence the whole
    # resolver relies on.
    return TickPlan(
        operations=(
            *stalls,
            *dispatch_ops,
            *ready_ops(items),
            *merge_ops(items, config),
        ),
        statuses=projected,
        admitted=tuple(dispatched),
        deferred=admission.deferred,
        notices=(*notices, *blocked_notices, *ci_notices(items)),
    )


def execute_tick(plan: TickPlan, api: GitHubApi) -> TickResult:
    dispatched = readied = merged = reclaimed = 0
    refused: list[Notice] = []
    for operation in plan.operations:
        match operation:
            case AssignAgent():
                api.assign_agent(number=operation.number, node_id=operation.node_id)
                dispatched += 1
            case UnassignAgent():
                api.unassign_agent(number=operation.number, assignees=operation.assignees)
                reclaimed += 1
            case LabelIssue():
                api.edit_labels(number=operation.number, add=operation.add, remove=operation.remove)
                dispatched += 1
            case MarkReady():
                api.mark_ready(number=operation.number)
                readied += 1
            case MergePr():
                try:
                    api.merge_pr(number=operation.number)
                except RuntimeError as exc:
                    refused.append(Notice("merge-refused", f"#{operation.number}", str(exc)))
                    continue
                merged += 1
    return TickResult(dispatched, readied, merged, tuple(refused), reclaimed)


def _dispatch_op(task: TaskItem) -> DispatchOperation | Notice:
    """Hand one task to its lane, or explain why it cannot be handed over."""
    if task.lane is Lane.LOCAL:
        # The scheduler cannot reach the workstation; the label *is* the
        # dispatch, and a daemon picks it up on its own schedule.
        return LabelIssue(task.ref, task.number, add=(LABEL_LOCAL_CLAIM,))
    if not task.node_id:
        return Notice(
            "missing-node-id",
            f"#{task.number}",
            "the snapshot carries no GraphQL node id, so the agent cannot be "
            "assigned; re-read the repository state",
        )
    return AssignAgent(task.ref, task.number, task.node_id)


def summarise(plan: TickPlan) -> Sequence[str]:
    """The pass's report: every task's status, then what it did about them.

    Every task, not only the ones being dispatched, because an idle pass has to
    explain itself — this is the whole of what a reader gets, so a pass that
    prints nothing has to be a pass that saw nothing.
    """
    lines = [f"  {ref}: {status.value}" for ref, status in plan.statuses.items()]
    handed = " ".join(str(ref) for ref in plan.admitted)
    lines.append("dispatch: " + (handed if plan.admitted else "(nothing)"))
    lines += [f"  defer {deferral}" for deferral in plan.deferred]
    lines += [f"NOTE {notice}" for notice in plan.notices]
    return lines
