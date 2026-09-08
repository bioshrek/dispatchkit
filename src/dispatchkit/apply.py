"""`apply` — reconcile a task graph into GitHub issues and Project items (D3).

Planning is a pure function of `(graph, RepoState)`; execution is a thin loop
that performs the planned operations and threads created issue numbers through
to the Project calls. Splitting them is what makes the idempotency claim
testable offline: apply, re-read, plan again, assert the plan is empty.

`id` is the idempotency key, carried in the issue's machine block rather than
in a side table, so a re-run updates the existing issue instead of creating a
twin. Three things `apply` refuses to do:

- **Touch a closed issue.** Closed means done; rewriting the body of finished
  work only invites confusion. Drift is reported instead.
- **Delete anything.** An issue whose task has left the graph is reported as an
  orphan for a human to close — the workflow token cannot delete issues, and
  silently closing work is not a decision a reconciler should make.
- **Write `Status` or `Attempts`.** They are derived scheduling state, owned by
  the scheduler and recomputed on every pass.
"""

from __future__ import annotations

from collections.abc import Mapping

from dispatchkit.block import parse_block, render_block
from dispatchkit.errors import GraphError
from dispatchkit.github import (
    APPLIED_FIELDS,
    DISPATCHKIT_LABEL,
    FIELD_LANE,
    FIELD_TASK_ID,
    FIELD_VERIFY,
    MANAGED_LABEL_PREFIXES,
    AddProjectItem,
    ApplyPlan,
    ApplyResult,
    CreateIssue,
    GitHubApi,
    IssueState,
    Notice,
    Operation,
    RepoState,
    SetProjectField,
    UpdateIssue,
)
from dispatchkit.model import Task, TaskGraph, TaskId


def desired_labels(task: Task, *, plan: str) -> tuple[str, ...]:
    return (
        DISPATCHKIT_LABEL,
        f"plan:{plan}",
        f"lane:{task.lane.value}",
        f"verify:{task.verify.value}",
    )


def build_body(task: Task, *, plan: str, spec: str | None = None) -> str:
    """The issue body: prose for a human, then the machine block for the scheduler.

    `acceptance` is injected verbatim so both lanes and the reviewer run the
    same check — the definition of done lives on the issue, not in a runner.
    """
    sections = [spec.strip() if spec and spec.strip() else task.title]
    sections.append(f"## Acceptance\n\n```sh\n{task.acceptance}\n```")
    sections.append(f"_Milestone {task.milestone}._")
    sections.append(render_block(task, plan=plan))
    return "\n\n".join(sections) + "\n"


def desired_fields(task: Task) -> dict[str, str]:
    return {
        FIELD_TASK_ID: str(task.id),
        FIELD_LANE: task.lane.value,
        FIELD_VERIFY: task.verify.value,
    }


def plan_apply(
    graph: TaskGraph, state: RepoState, specs: Mapping[TaskId, str] | None = None
) -> ApplyPlan:
    """Diff the graph against GitHub. Pure: no I/O, no client, no ordering surprises."""
    specs = specs or {}
    existing, notices = _index(state, plan=graph.plan)
    # Captured before the loop pops from `existing`. Every board item that
    # already exists, so a field operation on an issue an earlier run put on
    # the board has an id to write to.
    boarded = {
        task_id: issue.project_item_id
        for task_id, issue in existing.items()
        if issue.project_item_id is not None
    }
    operations: list[Operation] = []

    for task in graph.tasks:
        body = build_body(task, plan=graph.plan, spec=specs.get(task.id))
        labels = desired_labels(task, plan=graph.plan)
        issue = existing.pop(task.id, None)

        if issue is None:
            operations.append(CreateIssue(task.id, task.title, body, labels))
            operations.append(AddProjectItem(task.id, None))
            operations.extend(_field_ops(task, current={}))
            continue

        if issue.closed:
            # Closed is done. Report drift rather than rewriting finished work.
            if issue.title != task.title or issue.body != body:
                notices.append(
                    Notice(
                        "closed-drift",
                        str(task.id),
                        f"issue #{issue.number} is closed but the graph has changed since; "
                        "reopen it by hand if the work really needs redoing",
                    )
                )
            continue

        merged, stale = _merge_labels(issue.labels, labels)
        if (issue.title, issue.body, set(issue.labels)) != (task.title, body, set(merged)):
            operations.append(UpdateIssue(task.id, issue.number, task.title, body, merged, stale))

        if issue.project_item_id is None:
            operations.append(AddProjectItem(task.id, issue.number))
            operations.extend(_field_ops(task, current={}))
        else:
            operations.extend(_field_ops(task, current=issue.fields))

    for task_id, issue in existing.items():
        if not issue.closed:
            notices.append(
                Notice(
                    "orphan-issue",
                    str(task_id),
                    f"issue #{issue.number} carries task `{task_id}`, which is no longer "
                    "in the graph; "
                    "close it by hand — `apply` never deletes",
                )
            )
    return ApplyPlan(tuple(operations), tuple(notices), item_ids=boarded)


def execute_plan(plan: ApplyPlan, api: GitHubApi) -> ApplyResult:
    """Perform the planned operations, threading created numbers into Project calls."""
    labels = sorted({label for op in plan.operations for label in _labels_of(op)})
    if labels:
        api.ensure_labels(labels)

    numbers: dict[TaskId, int] = {}
    items: dict[TaskId, str] = dict(plan.item_ids)
    created = updated = added = fields_set = 0

    for operation in plan.operations:
        match operation:
            case CreateIssue():
                numbers[operation.task_id] = api.create_issue(
                    title=operation.title, body=operation.body, labels=list(operation.labels)
                )
                created += 1
            case UpdateIssue():
                api.update_issue(
                    number=operation.number,
                    title=operation.title,
                    body=operation.body,
                    labels=list(operation.labels),
                    remove_labels=list(operation.remove_labels),
                )
                numbers[operation.task_id] = operation.number
                updated += 1
            case AddProjectItem():
                resolved = (
                    operation.number if operation.number is not None else numbers[operation.task_id]
                )
                items[operation.task_id] = api.add_project_item(issue_number=resolved)
                added += 1
            case SetProjectField():
                api.set_project_field(
                    item_id=items[operation.task_id],
                    field_name=operation.field_name,
                    value=operation.value,
                )
                fields_set += 1

    return ApplyResult(created, updated, added, fields_set, tuple(numbers.values()))


def _index(state: RepoState, *, plan: str) -> tuple[dict[TaskId, IssueState], list[Notice]]:
    """Map task id → issue for this plan, reporting bodies we cannot read."""
    indexed: dict[TaskId, IssueState] = {}
    notices: list[Notice] = []

    for issue in state.issues:
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
                    "`apply` will not guess which task it belongs to",
                )
            )
            continue
        if block.plan != plan:
            continue
        indexed[block.id] = issue
    return indexed, notices


def _field_ops(task: Task, *, current: Mapping[str, str]) -> list[Operation]:
    wanted = desired_fields(task)
    return [
        SetProjectField(task.id, name, wanted[name])
        for name in APPLIED_FIELDS
        if current.get(name) != wanted[name]
    ]


def _merge_labels(
    existing: tuple[str, ...], wanted: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Replace the labels `apply` owns; leave everything else (`spend:approved`,
    `dispatch:stuck`, ...) exactly as a human or the scheduler left it."""
    owned = [
        label
        for label in existing
        if label == DISPATCHKIT_LABEL or label.startswith(MANAGED_LABEL_PREFIXES)
    ]
    kept = [label for label in existing if label not in owned]
    stale = tuple(label for label in owned if label not in wanted)
    return tuple(wanted) + tuple(kept), stale


def _labels_of(operation: Operation) -> tuple[str, ...]:
    match operation:
        case CreateIssue() | UpdateIssue():
            return operation.labels
        case _:
            return ()
