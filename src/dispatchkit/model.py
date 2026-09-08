"""Task graph value objects (D1).

Pure — no I/O, no third-party dependencies, mirroring the discipline
`src/podkit/domain` is held to. Parsing lives in `parse.py`, filesystem and
argv handling in `cli.py`, so the graph itself stays a data structure the
scheduler (D4) can reason about without touching GitHub.

See `docs/automation_task_dispatch_requirements.md`, "Task graph format".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import NewType

from dispatchkit.errors import GraphError, GraphIssue

TaskId = NewType("TaskId", str)

# `id` is the idempotency key for `apply` (D3) and is mirrored into labels and
# branch names, so it is restricted to a lowercase kebab slug.
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Lane(Enum):
    """Runner capability class — not subject matter, and not cost."""

    CLOUD = "cloud"
    LOCAL = "local"


class Verify(Enum):
    """Merge authority for the task's PR. `HUMAN` is the default."""

    AUTO = "auto"
    HUMAN = "human"


class Checks(Enum):
    """What CI is currently saying about a pull request (D5.6).

    `verify: auto` hands merge authority to CI, so the scheduler has to know
    whether CI actually holds an opinion. `BLOCKED` is the one that matters
    and the one that is easy to miss: GitHub treats a coding agent as an
    untrusted contributor and parks its workflow runs until a human approves
    them, which is not a failure and not a pending run — it is a pipeline that
    will never start on its own.
    """

    NONE = "none"
    PASSING = "passing"
    PENDING = "pending"
    FAILING = "failing"
    BLOCKED = "blocked"

    @property
    def stalled(self) -> bool:
        """Is a human the only thing that can move this PR forward?

        `NONE` is deliberately not a stall. Without stored state there is no
        way to distinguish "the runs do not exist yet" from "this repository
        has no CI", and the former is the normal state in the seconds after a
        PR opens. `BLOCKED` is GitHub saying so explicitly, so that is the
        signal acted on rather than an inference from absence.
        """
        return self in _STALLED

    @classmethod
    def combine(cls, parts: Iterable[Checks]) -> Checks:
        """The verdict for a PR whose suites each reported separately.

        Ordered by who has to act: a run nobody can start outranks one that
        failed, which outranks one still going. A green suite never raises the
        verdict, so one blocked suite is enough to stall the pull request.
        """
        return max(parts, key=_PRECEDENCE.index, default=cls.NONE)


_STALLED = frozenset({Checks.BLOCKED, Checks.FAILING})

#: Ascending severity. `combine` takes the maximum, so later wins.
_PRECEDENCE = [
    Checks.NONE,
    Checks.PASSING,
    Checks.PENDING,
    Checks.FAILING,
    Checks.BLOCKED,
]


@dataclass(frozen=True, slots=True)
class PullRequest:
    """An open PR linked to a task issue, with CI's current verdict on it."""

    number: int
    checks: Checks = Checks.NONE
    #: Read from `isDraft`, never inferred. GitHub documents a `DRAFT` value
    #: for `mergeStateStatus`, but a draft PR reports `CLEAN` there, so the
    #: summary field cannot be used to answer the one question that decides
    #: whether a merge is possible at all.
    draft: bool = False


# Capability tags a task may demand of its runner. The cloud lane advertises
# none of them, so any tag at all forces `local`.
CAPABILITIES: frozenset[str] = frozenset(
    {
        "long-run",
        "os:macos",
        "os:windows",
        "gpu",
        "device",
        "net:unrestricted",
        "local-data",
        "interactive",
    }
)


@dataclass(frozen=True, slots=True)
class Dependency:
    """One edge, which must name the artifact it waits on.

    `reason` is the TOML `for` key (`for` is a Python keyword). An edge that
    cannot name what it needs is ordering-by-narrative, and gets deleted.
    """

    on: TaskId
    reason: str


@dataclass(frozen=True, slots=True)
class Task:
    id: TaskId
    title: str
    milestone: str
    lane: Lane
    acceptance: str
    verify: Verify = Verify.HUMAN
    spend: bool = False
    requires: tuple[str, ...] = ()
    touches: tuple[str, ...] = ()
    depends: tuple[Dependency, ...] = ()
    body_file: str | None = None
    # Optional: where per-task estimates come from is an open question, so the
    # economic-floor lint (D2) stays silent rather than guessing when absent.
    estimate_minutes: int | None = None

    @property
    def cloud_eligible(self) -> bool:
        """A task may run in the cloud lane iff it demands no capability."""
        return not self.requires

    @property
    def routing(self) -> tuple[Lane, Verify, bool, tuple[str, ...]]:
        """What makes two adjacent tasks need different runners or gates."""
        return (self.lane, self.verify, self.spend, tuple(sorted(set(self.requires))))


@dataclass(frozen=True, slots=True)
class TaskGraph:
    plan: str
    tasks: tuple[Task, ...]

    def by_id(self) -> dict[TaskId, Task]:
        return {task.id: task for task in self.tasks}

    def edges(self) -> dict[TaskId, tuple[TaskId, ...]]:
        """Prerequisite ids per task, dangling targets dropped."""
        known = self.by_id()
        return {
            task.id: tuple(edge.on for edge in task.depends if edge.on in known)
            for task in self.tasks
        }

    def dependents(self) -> dict[TaskId, tuple[TaskId, ...]]:
        """Inverse of `edges()`: tasks unblocked by each task, in file order."""
        result: dict[TaskId, list[TaskId]] = {task.id: [] for task in self.tasks}
        for task_id, prerequisites in self.edges().items():
            for prerequisite in dict.fromkeys(prerequisites):
                result[prerequisite].append(task_id)
        return {task_id: tuple(unblocked) for task_id, unblocked in result.items()}

    def transitive_prerequisites(self) -> dict[TaskId, frozenset[TaskId]]:
        """Every task each task transitively waits on. Requires an acyclic graph."""
        edges = self.edges()
        closure: dict[TaskId, frozenset[TaskId]] = {}
        for task_id in self.topological_order():
            ancestors: set[TaskId] = set()
            for prerequisite in edges[task_id]:
                ancestors.add(prerequisite)
                ancestors |= closure[prerequisite]
            closure[task_id] = frozenset(ancestors)
        return closure

    def find_cycle(self) -> tuple[TaskId, ...]:
        """Return one cycle as `(a, b, ..., a)`, or `()` if the graph is acyclic."""
        edges = self.edges()
        state: dict[TaskId, int] = {}  # 0 = visiting, 1 = done
        stack: list[TaskId] = []

        def visit(node: TaskId) -> tuple[TaskId, ...]:
            state[node] = 0
            stack.append(node)
            for prerequisite in edges[node]:
                if state.get(prerequisite) == 0:
                    start = stack.index(prerequisite)
                    return (*stack[start:], prerequisite)
                if prerequisite not in state:
                    found = visit(prerequisite)
                    if found:
                        return found
            stack.pop()
            state[node] = 1
            return ()

        for task in self.tasks:
            if task.id not in state:
                cycle = visit(task.id)
                if cycle:
                    return cycle
        return ()

    def topological_order(self) -> tuple[TaskId, ...]:
        """Kahn's algorithm, tie-broken by file order so passes are reproducible.

        Raises `GraphError` on a cyclic graph; callers that want the cycle
        described rather than raised should use `validate_graph` instead.
        """
        position = {task.id: index for index, task in enumerate(self.tasks)}
        edges = self.edges()
        indegree = {task.id: len(set(edges[task.id])) for task in self.tasks}
        dependents: dict[TaskId, list[TaskId]] = {task.id: [] for task in self.tasks}
        for task_id, prerequisites in edges.items():
            for prerequisite in set(prerequisites):
                dependents[prerequisite].append(task_id)

        ready = sorted((t for t, d in indegree.items() if d == 0), key=position.__getitem__)
        order: list[TaskId] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for dependent in dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
            ready.sort(key=position.__getitem__)

        if len(order) != len(self.tasks):
            cycle = self.find_cycle()
            raise GraphError(
                [GraphIssue("cycle", self.plan, " -> ".join(cycle) or "cyclic graph")]
            )
        return tuple(order)
