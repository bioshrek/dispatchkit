"""Shared synthetic graph builders for the D2 lint/metric tests.

`docs/automation_task_dispatch_requirements.md` names the four shapes D2 is
proven against — chain, star, diamond, singleton — so they are built once here
rather than re-spelled in every test.
"""

from __future__ import annotations

from dispatchkit.model import Dependency, Lane, Task, TaskGraph, TaskId, Verify


def task(
    name: str,
    *,
    depends: tuple[str, ...] = (),
    lane: Lane = Lane.CLOUD,
    verify: Verify = Verify.HUMAN,
    spend: bool = False,
    requires: tuple[str, ...] = (),
    estimate_minutes: int | None = None,
) -> Task:
    return Task(
        id=TaskId(name),
        title=name,
        milestone="M",
        lane=lane,
        acceptance="true",
        verify=verify,
        spend=spend,
        requires=requires,
        depends=tuple(Dependency(TaskId(on), "artifact") for on in depends),
        estimate_minutes=estimate_minutes,
    )


def graph(*tasks: Task, plan: str = "demo") -> TaskGraph:
    return TaskGraph(plan=plan, tasks=tasks)


def singleton() -> TaskGraph:
    return graph(task("a"))


def chain(length: int = 4) -> TaskGraph:
    names = [f"t{i}" for i in range(length)]
    return graph(
        *(
            task(name, depends=() if i == 0 else (names[i - 1],))
            for i, name in enumerate(names)
        )
    )


def star(points: int = 4) -> TaskGraph:
    """Interface-first fan-out: one root, N independent implementations."""
    return graph(task("root"), *(task(f"leaf{i}", depends=("root",)) for i in range(points)))


def diamond() -> TaskGraph:
    return graph(
        task("root"),
        task("left", depends=("root",)),
        task("right", depends=("root",)),
        task("join", depends=("left", "right")),
    )
