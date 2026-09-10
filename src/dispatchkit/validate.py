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

import re
from collections import Counter
from collections.abc import Sequence

from dispatchkit.errors import GraphIssue
from dispatchkit.model import CAPABILITIES, SLUG, Lane, Task, TaskGraph, TaskId, Verify


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
        issues.append(GraphIssue("duplicate-capability", where, f"`requires` repeats {duplicates}"))

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


_RUN = re.compile(r"^(?P<indent>\s*)(?:-\s+)?run:\s*(?P<body>.*)$")


def ci_commands(text: str) -> tuple[str, ...]:
    """Every shell command a workflow runs, read off the text.

    Scanned rather than parsed because `src/dispatchkit` is pure standard
    library, and a YAML parser is not in it. Handles both `run: cmd` and a
    `run: |` block,
    and splits `&&` chains so each link can be matched on its own.
    """
    commands: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = _RUN.match(lines[index])
        index += 1
        if not match:
            continue
        body = match["body"].strip()
        if body not in {"|", ">", "|-", ">-"}:
            commands += _split(body)
            continue
        # A block scalar: every following line indented past the `run:` key.
        margin = len(match["indent"])
        while index < len(lines):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= margin:
                break
            commands += _split(line.strip())
            index += 1
    return tuple(command for command in commands if command)


def _split(body: str) -> list[str]:
    return [part.strip() for part in body.split("&&") if part.strip()]


def validate_acceptance(graph: TaskGraph, commands: Sequence[str]) -> list[GraphIssue]:
    """`verify = "auto"` is only honest if CI runs the task's acceptance.

    Without this, "all checks green" can mean an unrelated workflow passed
    while the acceptance command never ran — and `merge_ops` would merge on it.

    Coverage is prefix-based: CI's command must be the acceptance clause or a
    prefix of it, so a task may *narrow* what CI runs (`pytest -q` covers
    `pytest -q -k stopword`) but never widen it. That is a conservative
    approximation and it is wrong in the safe direction: it can refuse a task
    CI genuinely covers, whose remedy is to widen CI or use `verify = "human"`.
    Neither merges anything by mistake.
    """
    issues: list[GraphIssue] = []
    for task in graph.tasks:
        if task.verify is not Verify.AUTO:
            continue
        uncovered = [clause for clause in _split(task.acceptance) if not _covered(clause, commands)]
        if not uncovered:
            continue
        detail = (
            "this repository has no CI commands to check against"
            if not commands
            else "not run by CI: " + "; ".join(f"`{clause}`" for clause in uncovered)
        )
        issues.append(
            GraphIssue(
                "acceptance-not-in-ci",
                task.id,
                f'`verify = "auto"` merges on green CI, but {detail}. Widen CI, '
                'narrow `acceptance`, or use `verify = "human"`',
            )
        )
    return issues


def _covered(clause: str, commands: Sequence[str]) -> bool:
    return any(clause == command or clause.startswith(f"{command} ") for command in commands)


def triggers_on_pull_request(text: str) -> bool:
    """Does this workflow run on pull requests, and so report a check on one?

    Only these can gate a merge. A `schedule` workflow reports nothing on a
    pull request, and a `push: branches: [main]` one reports after the merge,
    which is too late to be a gate.

    Scanned rather than parsed, so it deliberately stops at `jobs:` — otherwise
    a step that merely mentions `pull_request` in its script would read as a
    trigger.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("jobs:"):
            return False
        if stripped.startswith(("pull_request:", "- pull_request", "pull_request_target:")):
            return True
        if stripped.startswith("on:") and "pull_request" in stripped:
            return True
    return False
