"""The README's example graph is a graph, not an illustration of one.

It had drifted: no `lane`, no `acceptance` on the second task, and a `depends`
entry keyed `reason` rather than `for`. Copying it produced five errors, which
is a poor first impression from a tool whose whole first step is `validate`.

Prose drifts from code silently; that is the same failure as a `touches` that
no longer describes the work, one file up. So the fix is not to correct the
snippet but to make the snippet checkable, and let CI notice next time.

Scoped to what the snippet can be right about on its own. Running
`dispatchkit validate` on it here also reports `acceptance-not-in-ci`, because
that check reads *this* repository's workflows and the example describes
somebody else's — a graph for another repo can never satisfy it, so it is not
a defect in the example and must not be "fixed" by rewriting the acceptance.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from dispatchkit.lints import lint_graph
from dispatchkit.parse import parse_graph
from dispatchkit.validate import validate_graph

pytestmark = pytest.mark.unit

README = Path(__file__).resolve().parent.parent / "README.md"


def first_toml_block() -> str:
    """The graph the README opens with, which is the one a reader copies."""
    lines = README.read_text(encoding="utf-8").splitlines()
    start = lines.index("```toml")
    end = lines.index("```", start + 1)
    return "\n".join(lines[start + 1 : end])


class TestTheExampleGraph:
    def test_it_is_valid_toml(self) -> None:
        assert tomllib.loads(first_toml_block())

    def test_it_parses_and_validates(self) -> None:
        # `parse_graph` raises with every issue at once, so a drifted example
        # fails here with the same list a reader would have seen.
        graph = parse_graph(first_toml_block(), plan="refactor")
        assert validate_graph(graph) == []

    def test_it_lints_clean(self) -> None:
        # An example that trips the tool's own warnings teaches the wrong
        # idiom -- `scope-omits-tests` above all, since the example is where
        # a reader learns what `touches` is for.
        graph = parse_graph(first_toml_block(), plan="refactor")
        assert [issue.code for issue in lint_graph(graph)] == []
