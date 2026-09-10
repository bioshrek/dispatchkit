"""Strict TOML → `TaskGraph` parsing (D1).

The schema is closed: unknown keys are rejected rather than ignored, because a
typo'd `verifiy = "auto"` that silently parses as "human" would be discovered
only by a merge that never happens. Structural problems (shape, types, enums)
are reported here; semantic invariants (ids, cycles, routing) live in
`validate.py`.
"""

from __future__ import annotations

import tomllib
from typing import Any

from dispatchkit.errors import GraphError, GraphIssue
from dispatchkit.model import Dependency, Lane, Task, TaskGraph, TaskId, Verify

__all__ = ["GraphError", "GraphIssue", "parse_graph"]

TOP_LEVEL_KEYS = frozenset({"doc", "plan", "task"})
REQUIRED_TASK_KEYS = ("id", "title", "milestone", "lane", "acceptance")
OPTIONAL_TASK_KEYS = (
    "verify",
    "spend",
    "requires",
    "touches",
    "depends",
    "body_file",
)
TASK_KEYS = frozenset(REQUIRED_TASK_KEYS) | frozenset(OPTIONAL_TASK_KEYS)
EDGE_KEYS = frozenset({"on", "for"})


class _Collector:
    def __init__(self) -> None:
        self.issues: list[GraphIssue] = []

    def add(self, code: str, where: str, message: str) -> None:
        self.issues.append(GraphIssue(code, where, message))

    def string(self, raw: dict[str, Any], key: str, where: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str):
            self.add("invalid-type", where, f"`{key}` must be a string, got {type_name(value)}")
            return ""
        return value

    def string_list(self, raw: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.add("invalid-type", where, f"`{key}` must be a list of strings")
            return ()
        return tuple(value)


def type_name(value: object) -> str:
    return type(value).__name__


def parse_graph(text: str, *, plan: str) -> TaskGraph:
    """Parse a `*.tasks.toml` document. Raises `GraphError` with every issue found."""
    collector = _Collector()
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise GraphError([GraphIssue("invalid-toml", plan, str(exc))]) from exc

    for key in sorted(set(document) - TOP_LEVEL_KEYS):
        collector.add("unknown-key", plan, f"unknown top-level key `{key}`")

    plan_name = plan
    if "plan" in document:
        if isinstance(document["plan"], str):
            plan_name = document["plan"]
        else:
            collector.add("invalid-type", plan, "`plan` must be a string")

    doc = None
    if "doc" in document:
        raw_doc = document["doc"]
        if isinstance(raw_doc, str) and raw_doc.strip():
            doc = raw_doc.strip()
        else:
            # A blank pointer renders a line telling the agent to read
            # nothing, which is worse than no line at all.
            collector.add("invalid-type", plan, "`doc` must be a non-empty string")

    raw_tasks = document.get("task", [])
    if not isinstance(raw_tasks, list) or not all(isinstance(item, dict) for item in raw_tasks):
        collector.add("invalid-type", plan, "`task` must be an array of tables")
        raise GraphError(collector.issues)
    if not raw_tasks:
        collector.add("empty-graph", plan, "no `[[task]]` entries — nothing to dispatch")
        raise GraphError(collector.issues)

    tasks = [_parse_task(raw, index, collector) for index, raw in enumerate(raw_tasks)]
    if collector.issues:
        raise GraphError(collector.issues)
    return TaskGraph(plan=plan_name, tasks=tuple(tasks), doc=doc)


def _parse_task(raw: dict[str, Any], index: int, collector: _Collector) -> Task:
    where = raw["id"] if isinstance(raw.get("id"), str) and raw["id"] else f"task[{index}]"

    for key in sorted(set(raw) - TASK_KEYS):
        collector.add("unknown-key", where, f"unknown task key `{key}`")
    for key in REQUIRED_TASK_KEYS:
        if key not in raw:
            collector.add("missing-key", where, f"missing required key `{key}`")

    body_file = raw.get("body_file")
    if body_file is not None and not isinstance(body_file, str):
        collector.add("invalid-type", where, "`body_file` must be a string")
        body_file = None

    spend = raw.get("spend", False)
    if not isinstance(spend, bool):
        collector.add("invalid-type", where, f"`spend` must be a boolean, got {type_name(spend)}")
        spend = False

    return Task(
        id=TaskId(collector.string(raw, "id", where) if "id" in raw else ""),
        title=collector.string(raw, "title", where) if "title" in raw else "",
        milestone=collector.string(raw, "milestone", where) if "milestone" in raw else "",
        lane=_parse_enum(Lane, raw, "lane", where, Lane.CLOUD, collector),
        acceptance=collector.string(raw, "acceptance", where) if "acceptance" in raw else "",
        verify=_parse_enum(Verify, raw, "verify", where, Verify.HUMAN, collector),
        spend=spend,
        requires=collector.string_list(raw, "requires", where),
        touches=collector.string_list(raw, "touches", where),
        depends=_parse_depends(raw, where, collector),
        body_file=body_file,
    )


def _parse_enum[E: (Lane, Verify)](
    enum: type[E],
    raw: dict[str, Any],
    key: str,
    where: str,
    default: E,
    collector: _Collector,
) -> E:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, str):
        collector.add("invalid-type", where, f"`{key}` must be a string")
        return default
    try:
        return enum(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        collector.add("invalid-enum", where, f"`{key}` must be one of {allowed}, got `{value}`")
        return default


def _parse_depends(
    raw: dict[str, Any], where: str, collector: _Collector
) -> tuple[Dependency, ...]:
    value = raw.get("depends", [])
    if not isinstance(value, list):
        collector.add("invalid-type", where, "`depends` must be an array of tables")
        return ()

    edges: list[Dependency] = []
    for edge in value:
        if not isinstance(edge, dict):
            collector.add(
                "invalid-type",
                where,
                "each `depends` entry must be a table like "
                '{ on = "task-id", for = "the artifact it waits on" }',
            )
            continue
        for key in sorted(set(edge) - EDGE_KEYS):
            collector.add("unknown-key", where, f"unknown `depends` key `{key}`")
        for key in sorted(EDGE_KEYS - set(edge)):
            collector.add("missing-key", where, f"`depends` entry is missing `{key}`")
        if EDGE_KEYS <= set(edge):
            on = collector.string(edge, "on", where)
            reason = collector.string(edge, "for", where)
            edges.append(Dependency(on=TaskId(on), reason=reason))
    return tuple(edges)
