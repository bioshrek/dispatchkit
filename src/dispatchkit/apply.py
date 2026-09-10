"""`apply` — reconcile a task graph into GitHub issues (D3).

Planning is a pure function of `(graph, RepoState)`; execution is a thin loop
that performs the planned operations. Splitting them is what makes the
idempotency claim testable offline: apply, re-read, plan again, assert the plan
is empty.

`id` is the idempotency key, carried in the issue's machine block rather than
in a side table, so a re-run updates the existing issue instead of creating a
twin. Three things `apply` refuses to do:

- **Touch a closed issue.** Closed means done; rewriting the body of finished
  work only invites confusion. Drift is reported instead.
- **Delete anything.** An issue whose task has left the graph is reported as an
  orphan for a human to close — the workflow token cannot delete issues, and
  silently closing work is not a decision a reconciler should make.
- **Record status anywhere.** It is derived scheduling state, recomputed from
  the issues on every pass and printed; the labels below mirror only what the
  machine block already says, which is a copy that cannot go stale.
"""

from __future__ import annotations

from collections.abc import Mapping

from dispatchkit.block import parse_block, render_block
from dispatchkit.errors import GraphError
from dispatchkit.github import (
    DISPATCHKIT_LABEL,
    MANAGED_LABEL_PREFIXES,
    ApplyPlan,
    ApplyResult,
    CreateIssue,
    GitHubApi,
    IssueState,
    Notice,
    Operation,
    RepoState,
    UpdateIssue,
)
from dispatchkit.model import DEFAULT_BASE, Task, TaskGraph, TaskId


def desired_labels(task: Task, *, plan: str) -> tuple[str, ...]:
    return (
        DISPATCHKIT_LABEL,
        f"plan:{plan}",
        f"lane:{task.lane.value}",
        f"verify:{task.verify.value}",
    )


def build_body(
    task: Task, *, plan: str, spec: str | None = None, doc: str | None = None
) -> str:
    """The issue body: prose for a human, then the machine block for the scheduler.

    `acceptance` is injected verbatim so both lanes and the reviewer run the
    same check — the definition of done lives on the issue, not in a runner.

    The `doc` pointer carries its own precedence. An agent handed two documents
    averages them, so the ranking is rendered here rather than left to a prompt
    template: the cloud lane has no template, and a rule that only one lane is
    told is not a rule.
    """
    sections = [spec.strip() if spec and spec.strip() else task.title]
    sections.append(f"## Acceptance\n\n```sh\n{task.acceptance}\n```")
    if doc:
        sections.append(
            f"_Background: `{doc}`. This issue is the contract and the document "
            "is context; where they say different things, follow this issue and "
            "note the disagreement in the pull request rather than reconciling "
            "them yourself._"
        )
    sections.append(f"_Milestone {task.milestone}._")
    sections.append(render_block(task, plan=plan))
    return "\n\n".join(sections) + "\n"


def plan_apply(
    graph: TaskGraph, state: RepoState, specs: Mapping[TaskId, str] | None = None
) -> ApplyPlan:
    """Diff the graph against GitHub. Pure: no I/O, no client, no ordering surprises."""
    specs = specs or {}
    existing, notices = _index(state, plan=graph.plan)
    operations: list[Operation] = []

    for task in graph.tasks:
        body = build_body(task, plan=graph.plan, spec=specs.get(task.id), doc=graph.doc)
        labels = desired_labels(task, plan=graph.plan)
        issue = existing.pop(task.id, None)

        if issue is None:
            operations.append(CreateIssue(task.id, task.title, body, labels))
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
    return ApplyPlan(tuple(operations), tuple(notices), graph.base)


def execute_plan(plan: ApplyPlan, api: GitHubApi) -> ApplyResult:
    """Perform the planned operations. Issues and the branch they integrate on.

    The branch is ensured before any issue is written, because an issue is a
    dispatchable instruction the moment it exists: a pass running concurrently
    could pick one up and try to cut a worktree from a branch that is not there
    yet. Nothing else here writes outside the issue list (D16).
    """
    if plan.base != DEFAULT_BASE:
        api.ensure_branch(base=plan.base)

    labels = sorted({label for op in plan.operations for label in _labels_of(op)})
    if labels:
        api.ensure_labels(labels)

    numbers: dict[TaskId, int] = {}
    created = updated = 0

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

    return ApplyResult(created, updated, tuple(numbers.values()))


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
