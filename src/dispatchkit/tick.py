"""D5: one scheduler pass — load, resolve, report, admit, dispatch.

Named `tick` for the unit rather than the command. The `tick` verb is gone —
`watch` is the command, and a pass is one iteration of its loop — but a tick is
still exactly what this module plans, so the name outlives the CLI it was taken
from.

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

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
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
from dispatchkit.model import Lane, TaskId, TaskRef
from dispatchkit.resolve import (
    LABEL_LOCAL_CLAIM,
    Deferral,
    Status,
    TaskItem,
    admit,
    blocking,
    build_items,
    ci_notices,
    merge_ops,
    ready_ops,
    resolve,
    stall_ops,
    stranded_notices,
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
    #: For each task, the dependencies it is still waiting on. Rendering only,
    #: like `statuses`: it answers "what would move this?" on the line that
    #: raised the question.
    blocked_on: Mapping[TaskRef, tuple[TaskId, ...]] = field(default_factory=dict)

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


#: The lanes this build can actually hand work to.
#:
#: `lane: local` is designed and unbuilt (D6), and marking a task for a lane
#: with no executor was worse than doing nothing: `dispatch:local` reads as a
#: claim, so the task reported `Dispatched` for ever, held the only local slot,
#: and kept its file scope reserved against tasks that could have run. Nothing
#: reclaimed it either — the stall timeout needs an assignee to time out.
#:
#: This is the one line D6 changes.
SERVED_LANES: frozenset[Lane] = frozenset({Lane.CLOUD})


def plan_tick(
    state: RepoState,
    *,
    config: SchedulerConfig,
    now: datetime,
    served: Collection[Lane] = SERVED_LANES,
) -> TickPlan:
    items, notices = build_items(state)
    statuses = resolve(items)
    admission = admit(items, statuses, config, served=served)

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
        notices=(
            *notices,
            *blocked_notices,
            *ci_notices(items),
            *stranded_notices(items),
            *_unserved_claims(items, served),
        ),
        blocked_on=blocking(items),
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


def _unserved_claims(items: Sequence[TaskItem], served: Collection[Lane]) -> tuple[Notice, ...]:
    """Report marks for a lane this build cannot serve.

    An earlier version handed these out, and repositories still carry them.
    The mark is a claim, so the task reads `Dispatched` and the report looks
    healthy while the lane is dead — which is the silence this whole tool
    exists to break.

    Reported, not repaired. Releasing an orphaned mark is startup
    reconciliation and it has to reconcile against the worktrees, so it arrives
    with the executor that creates them (D6); removing a label here on a guess
    is how a running task gets dispatched twice.
    """
    return tuple(
        Notice(
            "unserved-lane-claim",
            f"#{task.number}",
            f"{task.ref} is marked {LABEL_LOCAL_CLAIM} but nothing in this build runs "
            f"lane:{task.lane.value}, so it reads as dispatched and will never "
            f"finish. Remove the label to put it back in the queue.",
        )
        for task in items
        if not task.closed and task.lane not in served and LABEL_LOCAL_CLAIM in task.labels
    )


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


def summarise(plan: TickPlan, *, since: TickPlan | None = None) -> Sequence[str]:
    """The pass's report: what each task is, then what the pass did about it.

    With no `since` this is the whole picture, which is what a one-shot pass
    and the first pass of a loop both want — a pass that prints nothing must be
    a pass that saw nothing.

    With a `since` it is the difference from that pass, and an unchanged pass
    renders as nothing at all so the caller can print a heartbeat instead. That
    is not an optimisation: at a 60-second interval, work that takes twenty
    minutes produces twenty identical blocks, and the transition the reader is
    actually waiting for ends up buried in them.

    `since` is a previous plan held for rendering. It never reaches a decision
    — `plan_tick` does not take it — which is the line D13 drew around a
    long-running process keeping a memory at all.
    """
    if since is None:
        return _full(plan)
    if _content(plan) == _content(since):
        return ()
    return _delta(plan, since)


def _full(plan: TickPlan) -> list[str]:
    width = _column(plan.statuses)
    lines = [
        f"  {str(ref):<{width}}  {_annotate(plan, ref, status.value)}"
        for ref, status in plan.statuses.items()
    ]
    return lines + _actions(plan)


def _delta(plan: TickPlan, since: TickPlan) -> list[str]:
    """Only the tasks whose status is not what it was, plus what the pass did."""
    moved = {
        ref: (since.statuses.get(ref), status)
        for ref, status in plan.statuses.items()
        if since.statuses.get(ref) is not status
    }
    # A task whose issue disappeared between passes. Rare, and dropping it from
    # the report silently is exactly how it would go unnoticed.
    gone = [ref for ref in since.statuses if ref not in plan.statuses]

    width = _column({**dict.fromkeys(moved), **dict.fromkeys(gone)})
    lines = [
        f"  {str(ref):<{width}}  {_transition(plan, ref, was, now)}"
        for ref, (was, now) in moved.items()
    ]
    lines += [f"  {str(ref):<{width}}  gone (was {since.statuses[ref].value})" for ref in gone]
    return lines + _actions(plan)


def _transition(plan: TickPlan, ref: TaskRef, was: Status | None, now: Status) -> str:
    if was is None:
        return _annotate(plan, ref, f"new: {now.value}")
    return _annotate(plan, ref, f"{was.value} → {now.value}")


def _annotate(plan: TickPlan, ref: TaskRef, text: str) -> str:
    """Append what a blocked task is waiting on, if anything."""
    waiting = plan.blocked_on.get(ref, ())
    if not waiting:
        return text
    return f"{text}   ← {' '.join(waiting)}"


def _actions(plan: TickPlan) -> list[str]:
    handed = " ".join(str(ref) for ref in plan.admitted)
    lines = ["dispatch: " + (handed if plan.admitted else "(nothing)")]
    lines += [f"  defer {deferral}" for deferral in plan.deferred]
    lines += [f"NOTE {notice}" for notice in plan.notices]
    return lines


def _column(refs: Mapping[TaskRef, object]) -> int:
    """Width of the name column. A ragged list is unreadable at a glance."""
    return max((len(str(ref)) for ref in refs), default=0)


def _content(plan: TickPlan) -> tuple[object, ...]:
    """Everything the report says, and nothing else.

    Deferrals and notices are in here rather than only the statuses: a task
    deferred behind an open pull request stays deferred for as long as the pull
    request is open, so reprinting it every pass would mean the heartbeat never
    fires and the delta view buys nothing.
    """
    return (
        tuple(plan.statuses.items()),
        plan.admitted,
        plan.deferred,
        plan.notices,
        tuple(sorted(plan.blocked_on.items())),
    )
