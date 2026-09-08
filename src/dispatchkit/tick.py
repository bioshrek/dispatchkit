"""D5: one scheduler pass — load, resolve, reconcile, admit, dispatch.

`plan_tick` is pure: snapshot in, operations out. `execute_tick` is the only
thing that mutates, and the only mutation that matters is assignment, because
**assignment is the lock**. A task is ready only while unassigned, so the act
of dispatching removes it from the ready set. Two passes racing on the same
repository converge instead of double-dispatching, without a lease or a
lockfile — and a `replaceActorsForAssignable` that loses the race is a no-op
rather than a second assignee.

That is also why the operation order is load-bearing: dispatch first, record
second. Dying between the two leaves an assigned issue whose board entry is a
pass stale, which the next pass fixes; the reverse — a board claiming
`Dispatched` with nobody assigned — would let the next pass dispatch again.

A pass never creates, edits, or closes an issue. `apply` owns the graph's
shape, and only a merged PR closes work; the scheduler only ever hands work
out and tells the board what it sees.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

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
    SetProjectField,
)
from dispatchkit.model import Lane, TaskId
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
    reconcile_ops,
    resolve,
)


@dataclass(frozen=True, slots=True)
class TickPlan:
    operations: tuple[DispatchOperation, ...]
    #: Project item id per task, so the executor can write a field without
    #: re-reading the board it was just told about.
    item_ids: Mapping[TaskId, str]
    statuses: Mapping[TaskId, Status]
    admitted: tuple[TaskId, ...]
    deferred: tuple[Deferral, ...]
    notices: tuple[Notice, ...]

    def __bool__(self) -> bool:
        return bool(self.operations)


@dataclass(frozen=True, slots=True)
class TickResult:
    dispatched: int
    reconciled: int
    #: Pull requests taken out of draft. Kept apart from `dispatched` because
    #: no work was handed to an agent: an existing PR was merely un-drafted.
    readied: int = 0
    #: Pull requests squash-merged. The only count here that changed `main`.
    merged: int = 0


def plan_tick(state: RepoState, *, plan: str, config: SchedulerConfig) -> TickPlan:
    items, notices = build_items(state, plan=plan)
    statuses = resolve(items)
    admission = admit(items, statuses, config)

    by_id = {task.id: task for task in items}
    dispatch_ops: list[DispatchOperation] = []
    dispatched: list[TaskId] = []
    blocked_notices: list[Notice] = []

    for task_id in admission.admitted:
        operation = _dispatch_op(by_id[task_id])
        if isinstance(operation, Notice):
            blocked_notices.append(operation)
            continue
        dispatch_ops.append(operation)
        dispatched.append(task_id)

    # The board must not say `Ready` for an issue this pass just assigned, so
    # the projection is advanced for exactly the tasks that were handed out.
    # The next pass derives the same value from the assignee, so this converges
    # rather than needing a second write.
    projected = {**statuses, **dict.fromkeys(dispatched, Status.DISPATCHED)}

    # Planned from the state as read, so the board is told what was true when
    # we looked. A draft cleared by this pass becomes `Auto-merging` on the
    # next one, which is the same convergence the whole resolver relies on.
    return TickPlan(
        operations=(
            *dispatch_ops,
            *ready_ops(items),
            *merge_ops(items, config),
            *reconcile_ops(items, projected),
        ),
        item_ids={
            task.id: task.project_item_id
            for task in items
            if task.project_item_id is not None
        },
        statuses=projected,
        admitted=tuple(dispatched),
        deferred=admission.deferred,
        notices=(*notices, *blocked_notices, *ci_notices(items)),
    )


def execute_tick(plan: TickPlan, api: GitHubApi) -> TickResult:
    dispatched = reconciled = readied = merged = 0
    for operation in plan.operations:
        match operation:
            case AssignAgent():
                api.assign_agent(number=operation.number, node_id=operation.node_id)
                dispatched += 1
            case LabelIssue():
                api.edit_labels(
                    number=operation.number, add=operation.add, remove=operation.remove
                )
                dispatched += 1
            case MarkReady():
                api.mark_ready(number=operation.number)
                readied += 1
            case MergePr():
                api.merge_pr(number=operation.number)
                merged += 1
            case SetProjectField():
                _set_field(api, plan, operation)
                reconciled += 1
    return TickResult(dispatched, reconciled, readied, merged)


def _dispatch_op(task: TaskItem) -> DispatchOperation | Notice:
    """Hand one task to its lane, or explain why it cannot be handed over."""
    if task.lane is Lane.LOCAL:
        # The scheduler cannot reach the workstation; the label *is* the
        # dispatch, and a daemon picks it up on its own schedule.
        return LabelIssue(task.id, task.number, add=(LABEL_LOCAL_CLAIM,))
    if not task.node_id:
        return Notice(
            "missing-node-id",
            f"#{task.number}",
            "the snapshot carries no GraphQL node id, so the agent cannot be "
            "assigned; re-read the repository state",
        )
    return AssignAgent(task.id, task.number, task.node_id)


def _set_field(api: GitHubApi, plan: TickPlan, operation: SetProjectField) -> None:
    item_id = plan.item_ids.get(operation.task_id)
    if item_id is None:  # pragma: no cover - reconcile_ops only emits for known items
        return
    api.set_project_field(
        item_id=item_id, field_name=operation.field_name, value=operation.value
    )


def summarise(plan: TickPlan) -> Sequence[str]:
    """Human-readable lines for the CLI and the workflow log."""
    lines = [f"  {task_id}: {status.value}" for task_id, status in plan.statuses.items()]
    lines.append("dispatch: " + (" ".join(plan.admitted) if plan.admitted else "(nothing)"))
    lines += [f"  defer {deferral}" for deferral in plan.deferred]
    lines += [f"NOTE {notice}" for notice in plan.notices]
    return lines
