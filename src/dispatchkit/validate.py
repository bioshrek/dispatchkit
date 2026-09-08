"""Semantic invariants over a parsed graph (D1).

Everything here is checked *before* anything reaches GitHub, because the graph
file — not the issue tracker — is the reviewable unit: unique ids, every
`depends` resolving, an acyclic graph, a non-empty `for` on every edge, a
non-empty `acceptance`, and the capability-based lane routing rule.

Returns a list rather than raising, so a reviewer sees every problem at once.
Structural lints and plan-shape metrics are deliberately *not* here — they are
D2, and they are warnings rather than errors.
"""

from __future__ import annotations

from collections import Counter

from dispatchkit.errors import GraphIssue
from dispatchkit.model import CAPABILITIES, SLUG, Lane, Task, TaskGraph, TaskId


def validate_graph(graph: TaskGraph) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    known = graph.by_id()

    for task in graph.tasks:
        issues.extend(_validate_task(task))
        issues.extend(_validate_edges(task, known))

    for task_id, count in Counter(task.id for task in graph.tasks).items():
        if count > 1:
            issues.append(
                GraphIssue("duplicate-id", task_id, f"`id` is used by {count} tasks; ids are keys")
            )

    # A dangling or self edge already explains the malformed shape; reporting a
    # cycle on top of it would be noise.
    if not any(i.code in {"dangling-dependency", "self-dependency"} for i in issues):
        cycle = graph.find_cycle()
        if cycle:
            issues.append(
                GraphIssue("cycle", graph.plan, "dependency cycle: " + " -> ".join(cycle))
            )
    return issues


def _validate_task(task: Task) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    where = task.id or "<unnamed task>"

    if not SLUG.match(task.id):
        issues.append(
            GraphIssue("invalid-id", where, f"`id` must be a lowercase kebab slug, got `{task.id}`")
        )
    for field in ("title", "milestone"):
        if not getattr(task, field).strip():
            issues.append(GraphIssue("empty-field", where, f"`{field}` must be non-empty"))
    if not task.acceptance.strip():
        issues.append(
            GraphIssue(
                "empty-acceptance",
                where,
                "`acceptance` must be a command with a binary exit code — no AC, no task",
            )
        )

    if task.estimate_minutes is not None and task.estimate_minutes <= 0:
        issues.append(
            GraphIssue(
                "invalid-estimate",
                where,
                f"`estimate_minutes` must be positive, got {task.estimate_minutes}",
            )
        )

    unknown = [tag for tag in task.requires if tag not in CAPABILITIES]
    if unknown:
        issues.append(
            GraphIssue(
                "unknown-capability",
                where,
                f"no runner advertises {unknown}; known tags: {sorted(CAPABILITIES)}",
            )
        )
    duplicates = sorted({tag for tag, n in Counter(task.requires).items() if n > 1})
    if duplicates:
        issues.append(
            GraphIssue("duplicate-capability", where, f"`requires` repeats {duplicates}")
        )

    # The routing rule, stated once: cloud iff no capability is demanded.
    if task.lane is Lane.CLOUD and not task.cloud_eligible:
        issues.append(
            GraphIssue(
                "lane-capability-mismatch",
                where,
                f"lane `cloud` cannot satisfy requires {list(task.requires)}; use lane `local`",
            )
        )
    return issues


def _validate_edges(task: Task, known: dict[TaskId, Task]) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    where = task.id or "<unnamed task>"
    seen: set[TaskId] = set()

    for edge in task.depends:
        if not edge.reason.strip():
            issues.append(
                GraphIssue(
                    "empty-edge-reason",
                    where,
                    f"edge on `{edge.on}` must name the artifact it waits on; "
                    "an edge that cannot is spurious and should be deleted",
                )
            )
        if edge.on == task.id:
            issues.append(GraphIssue("self-dependency", where, "a task cannot depend on itself"))
        elif edge.on not in known:
            issues.append(
                GraphIssue(
                    "dangling-dependency", where, f"`depends` names unknown task `{edge.on}`"
                )
            )
        if edge.on in seen:
            issues.append(
                GraphIssue("duplicate-dependency", where, f"`{edge.on}` is depended on twice")
            )
        seen.add(edge.on)
    return issues
