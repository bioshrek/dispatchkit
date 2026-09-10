"""`docs/schema.md` describes the schema the code actually implements.

A reference page is the one document a reader trusts literally: they copy the
example and they believe the key lists. So the failure mode is not an
embarrassing typo, it is a reader who declares `estimate_minutes` because the
page still lists it, and gets an error from a tool whose first instruction was
to run `validate`.

Prose drifts from code silently -- the same failure as a `touches` that no
longer describes the work. The fix is not to proofread the page but to make it
checkable, and let CI notice next time. So: the example must parse, validate
and lint clean, and the two key tables and the two code lists must be exactly
what `parse`, `validate` and `lints` know about.

The `acceptance-not-in-ci` exclusion is the same one `test_readme_example.py`
carries: that check reads *this* repository's workflows and the example
describes somebody else's, so it can never be satisfied here and must not be
"fixed" by rewriting the example's acceptance.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from dispatchkit.lints import lint_graph
from dispatchkit.parse import OPTIONAL_TASK_KEYS, REQUIRED_TASK_KEYS, TOP_LEVEL_KEYS, parse_graph
from dispatchkit.validate import validate_graph

pytestmark = pytest.mark.unit

SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "schema.md"


def text() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def example() -> str:
    """The full plan the page closes with, which is the one a reader copies."""
    lines = text().split("## A minimal plan", 1)[1].splitlines()
    start = lines.index("```toml")
    end = lines.index("```", start + 1)
    return "\n".join(lines[start + 1 : end])


def section(heading: str) -> str:
    body = text().split(heading, 1)[1]
    return re.split(r"\n#{2,4} |\n<!-- ", body, maxsplit=1)[0]


def documented_keys(heading: str) -> set[str]:
    """The first column of the key table under a heading -- the rows a reader
    reads as the list of keys, rather than any backtick in the surrounding
    prose."""
    return set(re.findall(r"^\| `([a-z_]+)`", section(heading), flags=re.MULTILINE))


def listed_codes(heading: str) -> set[str]:
    # Both code sections are bounded so that every backticked lowercase token
    # inside one really is a code, `--strict` excluded by the leading dash.
    return set(re.findall(r"`([a-z][a-z-]*)`", section(heading)))


class TestTheExampleIsAPlan:
    def test_it_is_toml(self) -> None:
        assert tomllib.loads(example())

    def test_it_parses(self) -> None:
        graph = parse_graph(example(), plan="widget")

        assert graph.plan == "widget"
        assert graph.doc == "docs/design.md"
        assert len(graph.tasks) == 3

    def test_it_validates(self) -> None:
        graph = parse_graph(example(), plan="widget")

        codes = {issue.code for issue in validate_graph(graph)}

        assert codes <= {"acceptance-not-in-ci"}

    def test_it_lints_clean(self) -> None:
        # Including the body lints: the example declares `body_file` on every
        # task, because a page teaching the contract cannot open by breaking it.
        graph = parse_graph(example(), plan="widget")
        specs = {task.id: "The brief." for task in graph.tasks}

        assert lint_graph(graph, specs=specs) == []

    def test_it_exercises_the_keys_it_documents(self) -> None:
        # An example that shows only the required keys teaches that the
        # optional ones are exotic. They are not; `touches` and `body_file`
        # are how the plan earns its scheduling and its briefs.
        used = {"doc", "plan"}
        for raw in tomllib.loads(example())["task"]:
            used |= set(raw)

        assert {"touches", "body_file", "depends", "verify"} <= used


class TestTheTablesMatchTheCode:
    def test_the_top_level_table_is_exactly_the_top_level_keys(self) -> None:
        assert documented_keys("## Top-level keys") == set(TOP_LEVEL_KEYS)

    def test_the_required_table_is_exactly_the_required_keys(self) -> None:
        assert documented_keys("### Required") == set(REQUIRED_TASK_KEYS)

    def test_the_optional_table_is_exactly_the_optional_keys(self) -> None:
        # Equality in both directions. A key the parser gained and the page
        # never mentioned is invisible; a key the page lists and the parser
        # rejects is worse, because the reader copies it and gets an error.
        assert documented_keys("### Optional") == set(OPTIONAL_TASK_KEYS)


class TestTheCodeListsAreCurrent:
    def test_the_warning_list_is_exactly_the_lints(self) -> None:
        from dispatchkit import lints

        real = set(
            re.findall(
                r'GraphIssue\(\s*"([a-z-]+)"',
                Path(lints.__file__).read_text(encoding="utf-8"),
            )
        )

        assert listed_codes("**Warnings** report") == real

    def test_the_error_list_is_exactly_what_stops_a_push(self) -> None:
        from dispatchkit import parse, validate

        real = set()
        for module in (parse, validate):
            source = Path(module.__file__ or "").read_text(encoding="utf-8")
            real |= set(re.findall(r'(?:GraphIssue|add)\(\s*"([a-z-]+)"', source))

        assert listed_codes("**Errors** stop the push") == real
