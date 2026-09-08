"""Structural lints (D2).

Warnings, never errors. `validate_graph` (D1) decides whether a graph may be
pushed; these decide nothing — they report the signals that make a human's
ratification of the decomposition informed:

| Signal                                         | Code                   |
| ---------------------------------------------- | ---------------------- |
| `count == 1`                                   | `no-decomposition`     |
| `width == 1`                                   | `chain-graph`          |
| `depth` close to `count`                       | `mostly-serial`        |
| Serial pair, no other dependents, same routing | `merge-candidate`      |
| Estimated work below ~3x overhead              | `under-economic-floor` |

The cost model behind the last three: makespan is roughly critical-path length
x (work + overhead), and overhead is fixed and far from free. Splitting a node
into two parallel nodes shortens the schedule; splitting it into two serial
nodes lengthens it by exactly one overhead and buys nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from dispatchkit.errors import GraphIssue
from dispatchkit.metrics import plan_shape
from dispatchkit.model import TaskGraph


@dataclass(frozen=True, slots=True)
class LintConfig:
    # `depth / count` at or above this reads as "mostly serial".
    serial_ratio: float = 0.8
    # Issue, agent boot, clone, install, CI, merge queue — per dispatched task.
    overhead_minutes: int = 10
    # A task should be worth several times the overhead it pays.
    floor_multiple: int = 3

    @property
    def economic_floor_minutes(self) -> int:
        return self.overhead_minutes * self.floor_multiple


DEFAULT_LINTS = LintConfig()


def lint_graph(graph: TaskGraph, config: LintConfig = DEFAULT_LINTS) -> list[GraphIssue]:
    """Report structural warnings. Never raises, even on an invalid graph."""
    issues: list[GraphIssue] = []
    is_chain = False

    if not graph.find_cycle():  # shape metrics are undefined on a cyclic graph
        shape = plan_shape(graph)
        is_chain = shape.count > 1 and shape.width == 1

        if shape.count == 1:
            issues.append(
                GraphIssue(
                    "no-decomposition",
                    graph.plan,
                    "a single task is not a decomposition; either split it or dispatch it by hand",
                )
            )
        if is_chain:
            issues.append(
                GraphIssue(
                    "chain-graph",
                    graph.plan,
                    f"width 1 over {shape.count} tasks — a chain wearing a graph's clothes; "
                    "every task pays dispatch overhead and none of it runs concurrently",
                )
            )
        elif shape.count >= 3 and shape.depth >= config.serial_ratio * shape.count:
            issues.append(
                GraphIssue(
                    "mostly-serial",
                    graph.plan,
                    f"depth {shape.depth} of {shape.count} tasks — look for spurious edges; "
                    "an edge that cannot name the artifact it waits on should be deleted",
                )
            )

    if not is_chain:  # on a chain, `chain-graph` already says it, once
        issues.extend(_merge_candidates(graph))
    issues.extend(_economic_floor(graph, config))
    return issues


def _merge_candidates(graph: TaskGraph) -> list[GraphIssue]:
    """Serial pair, no other dependents, same routing — the split earns nothing."""
    known = graph.by_id()
    edges = graph.edges()
    dependents = graph.dependents()
    issues: list[GraphIssue] = []

    for task in graph.tasks:
        prerequisites = set(edges[task.id])
        if len(prerequisites) != 1:
            continue
        (only,) = prerequisites
        if dependents[only] != (task.id,):
            continue
        if known[only].routing != task.routing:
            continue  # routing or verification differs, so the boundary earns itself
        issues.append(
            GraphIssue(
                "merge-candidate",
                task.id,
                f"`{only}` -> `{task.id}` is a serial pair with identical routing and no "
                "other dependents; merging saves one dispatch overhead and loses nothing",
            )
        )
    return issues


def _economic_floor(graph: TaskGraph, config: LintConfig) -> list[GraphIssue]:
    floor = config.economic_floor_minutes
    return [
        GraphIssue(
            "under-economic-floor",
            task.id,
            f"estimated {task.estimate_minutes}m is below the ~{floor}m floor "
            f"({config.floor_multiple}x the {config.overhead_minutes}m dispatch overhead)",
        )
        for task in graph.tasks
        if task.estimate_minutes is not None and task.estimate_minutes < floor
    ]
