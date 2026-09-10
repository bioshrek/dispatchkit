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
| Acceptance runs tests, `touches` names none    | `scope-omits-tests`    |
| No `body_file`, so the brief is the title      | `thin-body`            |
| Body names a path `touches` does not cover     | `scope-omits-named-path` |

The cost model behind the shape lints: makespan is roughly critical-path length
x (work + overhead), and overhead is fixed and far from free. Splitting a node
into two parallel nodes shortens the schedule; splitting it into two serial
nodes lengthens it by exactly one overhead and buys nothing.

There was a fifth, `under-economic-floor`, comparing a task's declared
`estimate_minutes` against a multiple of that overhead. It went with the key in
D10: the estimate was an unreviewed guess, so the lint was arithmetic over a
number the author had invented. D15 measures the same quantity from the
timeline, where it is a fact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch

from dispatchkit.errors import GraphIssue
from dispatchkit.metrics import plan_shape
from dispatchkit.model import Task, TaskGraph, TaskId


@dataclass(frozen=True, slots=True)
class LintConfig:
    # `depth / count` at or above this reads as "mostly serial".
    serial_ratio: float = 0.8


DEFAULT_LINTS = LintConfig()


def lint_graph(
    graph: TaskGraph,
    config: LintConfig = DEFAULT_LINTS,
    *,
    specs: Mapping[TaskId, str] | None = None,
) -> list[GraphIssue]:
    """Report structural warnings. Never raises, even on an invalid graph.

    `specs` maps a task to the prose of its `body_file`, which lives outside
    the graph and so has to be handed in — this module opens nothing. `None`
    means the caller did not read them and the body lints stay quiet; an empty
    mapping means it did, and found none.
    """
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
    issues.extend(_scope_omits_tests(graph))
    if specs is not None:
        issues.extend(_body_lints(graph, specs))
    return issues


#: Commands that pass only when a test does. Matched as whole words, so
#: `attest`, `latest` and `contest` are not test runners.
_TEST_RUNNERS = (
    "pytest",
    "unittest",
    "jest",
    "vitest",
    "rspec",
    "tox",
    "nose2",
    "phpunit",
    "ctest",
    "test",  # `npm test`, `go test`, `cargo test`, `mix test`, `dotnet test`
)

#: Path fragments that a test lives under, across the ecosystems above.
_TEST_PATHS = ("test", "tests", "spec", "specs", "__tests__", "_test", "e2e")


def _runs_tests(acceptance: str) -> str | None:
    """The test runner this acceptance invokes, if it invokes one."""
    words = set(re.findall(r"[a-z0-9_]+", acceptance.lower()))
    return next((runner for runner in _TEST_RUNNERS if runner in words), None)


def _covers_tests(touches: Sequence[str]) -> bool:
    """Could any declared pattern admit a test file?

    A pattern is read generously — `**` and `src/*` admit anything under them,
    and refusing to see that would make the lint fire on scopes that are wide
    rather than wrong.
    """
    for pattern in touches:
        if set(pattern) <= {"*", "/"}:
            return True
        parts = re.split(r"[/._-]", pattern.lower())
        if any(part in _TEST_PATHS for part in parts):
            return True
    return False


def _scope_omits_tests(graph: TaskGraph) -> list[GraphIssue]:
    """The plan states its scope twice and the two copies disagree.

    An acceptance that runs a test suite is satisfied by a test file, so a
    `touches` with no test path in it is a declaration that contradicts the
    definition of done sitting beside it. `document-flags` shipped exactly
    that, and its agent -- told the acceptance and never the scope -- wrote
    the test the acceptance demanded and drifted out of its own declaration.

    Silent on an empty `touches`: there is no second copy to disagree with,
    and since D9.2 an undeclared scope costs only a weaker exclusion.

    A task that merely re-runs an existing suite without adding to it will
    trip this, which is why the message says what was observed rather than
    what to do -- one of the two statements is wrong, and only the author
    knows which.
    """
    issues: list[GraphIssue] = []
    for task in graph.tasks:
        if not task.touches or _covers_tests(task.touches):
            continue
        runner = _runs_tests(task.acceptance)
        if runner is None:
            continue
        issues.append(
            GraphIssue(
                "scope-omits-tests",
                task.id,
                f"acceptance runs `{runner}` but `touches` declares no test path "
                f"({', '.join(task.touches)}); if the task writes the test its acceptance "
                "needs, the declaration is already wrong and the exclusion will schedule "
                "on it",
            )
        )
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




#: A backticked token is treated as a repository path only if it contains a
#: slash. Bodies are full of backticked things that are not files -- `--top`,
#: `uv run pytest -q`, `v0.2` -- and requiring a separator admits none of
#: them while still catching `tests/test_readme.py`, which is the case that
#: prompted the lint. A bare `README.md` in prose is missed, deliberately: the
#: alternative is a list of file extensions, which is a list of guesses.
#: The token is matched whole and the slash tested afterwards, rather than
#: written as `[^`\s]+/[^`\s]+`. That form has two greedy runs that both
#: admit a slash, so an unterminated backtick followed by a long run of them
#: backtracks quadratically. Nothing here is attacker-reachable -- a
#: `body_file` is local, unlike an issue body -- but a linear pattern costs
#: nothing and removes the question.
_PATH_IN_PROSE = re.compile(r"`([^`\s]+)`")


def _paths_in(prose: str) -> list[str]:
    """Backticked tokens holding an interior slash, in order, deduplicated.

    Interior: a slash with at least one character on each side, which is what
    the two-run form matched. `/etc` and `a/` are not paths by this rule.
    """
    return list(dict.fromkeys(t for t in _PATH_IN_PROSE.findall(prose) if "/" in t[1:-1]))

#: Fenced blocks are illustration -- worked examples, commands, diffs -- and
#: the paths inside them are things to read, not things the task will change.
#:
#: Leading whitespace is allowed on both fences because a code block under a
#: numbered step is ordinary markdown, and it is the shape the body contract's
#: "definition of done" section invites. An unterminated fence runs to the end
#: of the body: a dropped closing fence is a typo in the brief, and reading the
#: rest of a code block as prose turns that typo into a lint failure under
#: `--strict`.
_FENCE = re.compile(r"^[ \t]*```.*?(?:^[ \t]*```|\Z)", re.M | re.S)


def _body_lints(graph: TaskGraph, specs: Mapping[TaskId, str]) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    for task in graph.tasks:
        prose = specs.get(task.id, "").strip()
        if not prose:
            issues.append(
                GraphIssue(
                    "thin-body",
                    task.id,
                    "no `body_file`, so the issue body is its title and the acceptance; the "
                    "prompt is that prose, so the agent is told what to call the work but not "
                    "what it is",
                )
            )
            continue
        issues.extend(_named_paths_outside_scope(task, prose))
    return issues


def _named_paths_outside_scope(task: Task, prose: str) -> list[GraphIssue]:
    """A path the brief names that the declared scope does not admit.

    The third place a plan states its scope, after `touches` and the
    acceptance, and the only one the agent actually reads. `document-flags`
    asked in prose for a file its `touches` did not admit, and the two
    disagreed all the way to a withheld merge.

    Silent on an empty `touches`: there is nothing to disagree with.
    """
    if not task.touches:
        return []
    missing = [
        path
        for path in _paths_in(_FENCE.sub("", prose))
        if "://" not in path and not any(fnmatch(path, pattern) for pattern in task.touches)
    ]
    if not missing:
        return []
    return [
        GraphIssue(
            "scope-omits-named-path",
            task.id,
            f"the body names {', '.join(f'`{path}`' for path in missing)}, which `touches` "
            "does not cover; the prose is what the agent is given, so if the task changes "
            "those files the declaration is already wrong",
        )
    ]
