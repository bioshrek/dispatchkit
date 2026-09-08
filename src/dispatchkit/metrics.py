"""Plan-shape metrics (D2).

Pure functions over a validated graph. The shape line — `12 tasks, depth 4,
width 5, 3 human-verify, est. 2 review sessions` — is what turns the review
gate from a vibe check into a quantitative one.

`width` is the **maximum antichain**: the largest set of tasks that are
pairwise independent and could therefore all be in flight at once. It is
computed exactly, via Dilworth's theorem (max antichain = n − maximum
bipartite matching on the transitive closure) rather than by taking the widest
layer of a longest-path layering. The layered figure is only a lower bound, and
under-reporting parallelism would defeat the purpose of the metric.
"""

from __future__ import annotations

from dataclasses import dataclass

from dispatchkit.model import Lane, TaskGraph, TaskId, Verify


@dataclass(frozen=True, slots=True)
class PlanShape:
    count: int
    depth: int  # tasks on the critical path
    width: int  # max antichain: how wide the graph can actually run
    human_verify: int
    auto_verify: int
    cloud: int
    local: int
    spend: int
    review_sessions: int

    def summary(self) -> str:
        return (
            f"{self.count} tasks, depth {self.depth}, width {self.width}, "
            f"{self.human_verify} human-verify, est. {self.review_sessions} review sessions"
        )


def levels(graph: TaskGraph) -> dict[TaskId, int]:
    """Longest-path level per task: 0 for a task with no prerequisites.

    Longest, not shortest — a task is only reachable once its slowest
    prerequisite chain has landed. Raises `GraphError` on a cyclic graph.
    """
    edges = graph.edges()
    level: dict[TaskId, int] = {}
    for task_id in graph.topological_order():
        prerequisites = edges[task_id]
        level[task_id] = 1 + max((level[p] for p in prerequisites), default=-1)
    return level


def max_antichain(graph: TaskGraph) -> tuple[TaskId, ...]:
    """A largest set of pairwise-independent tasks, in file order.

    Returned as the witness rather than just its size, so a report can name
    which tasks the width claim rests on.
    """
    order = [task.id for task in graph.tasks]
    successors = _transitive_successors(graph)

    matched_left: dict[TaskId, TaskId] = {}
    matched_right: dict[TaskId, TaskId] = {}
    for node in order:
        _augment(node, successors, matched_left, matched_right, set())

    # König: alternating search from the unmatched left vertices yields a
    # minimum vertex cover, whose complement is a maximum antichain.
    seen_left: set[TaskId] = set()
    seen_right: set[TaskId] = set()
    for node in order:
        if node not in matched_left:
            _alternate(node, successors, matched_right, seen_left, seen_right)
    return tuple(n for n in order if n in seen_left and n not in seen_right)


def plan_shape(graph: TaskGraph) -> PlanShape:
    level = levels(graph)
    human = [task for task in graph.tasks if task.verify is Verify.HUMAN]
    return PlanShape(
        count=len(graph.tasks),
        depth=max(level.values(), default=-1) + 1,
        width=len(max_antichain(graph)),
        human_verify=len(human),
        auto_verify=sum(1 for task in graph.tasks if task.verify is Verify.AUTO),
        cloud=sum(1 for task in graph.tasks if task.lane is Lane.CLOUD),
        local=sum(1 for task in graph.tasks if task.lane is Lane.LOCAL),
        spend=sum(1 for task in graph.tasks if task.spend),
        # Human review is batched: everything reviewable at the same level is
        # one sitting, so sessions count levels rather than tasks.
        review_sessions=len({level[task.id] for task in human}),
    )


def _transitive_successors(graph: TaskGraph) -> dict[TaskId, frozenset[TaskId]]:
    prerequisites = graph.transitive_prerequisites()
    successors: dict[TaskId, set[TaskId]] = {task.id: set() for task in graph.tasks}
    for task_id, ancestors in prerequisites.items():
        for ancestor in ancestors:
            successors[ancestor].add(task_id)
    return {node: frozenset(reachable) for node, reachable in successors.items()}


def _augment(
    left: TaskId,
    successors: dict[TaskId, frozenset[TaskId]],
    matched_left: dict[TaskId, TaskId],
    matched_right: dict[TaskId, TaskId],
    visited: set[TaskId],
) -> bool:
    """One Hungarian augmenting-path step over the comparability relation."""
    for right in sorted(successors[left]):
        if right in visited:
            continue
        visited.add(right)
        holder = matched_right.get(right)
        if holder is None or _augment(holder, successors, matched_left, matched_right, visited):
            matched_right[right] = left
            matched_left[left] = right
            return True
    return False


def _alternate(
    left: TaskId,
    successors: dict[TaskId, frozenset[TaskId]],
    matched_right: dict[TaskId, TaskId],
    seen_left: set[TaskId],
    seen_right: set[TaskId],
) -> None:
    if left in seen_left:
        return
    seen_left.add(left)
    for right in sorted(successors[left]):
        if right in seen_right:
            continue
        seen_right.add(right)
        holder = matched_right.get(right)
        if holder is not None:
            _alternate(holder, successors, matched_right, seen_left, seen_right)
