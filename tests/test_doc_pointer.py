"""The optional top-level `doc` key: one reference, and the precedence for it.

An agent handed two documents will average them. So the pointer is rendered
with its precedence stated -- the issue is the contract, the plan is
background -- rather than left for a prompt template to explain, because the
cloud lane has no template and would otherwise get the worse deal.

The reference is prose for a language model. It gets no machine-block key and
no version bump, and it is deliberately unpinned: a plan edited between review
and run gives the agent a brief nobody approved, which is accepted because the
part that binds is on the issue.
"""

from __future__ import annotations

import pytest

from dispatchkit.apply import build_body
from dispatchkit.errors import GraphError
from dispatchkit.parse import parse_graph

pytestmark = pytest.mark.unit


ONE_TASK = """
[[task]]
id = "a"
title = "A"
milestone = "M"
lane = "cloud"
acceptance = "uv run pytest"
"""


def parse(text: str, plan: str = "demo"):  # type: ignore[no-untyped-def]
    return parse_graph(text, plan=plan)


class TestTheKeyParses:
    def test_a_graph_may_name_a_document(self) -> None:
        graph = parse('doc = "docs/design.md"\n' + ONE_TASK)

        assert graph.doc == "docs/design.md"

    def test_omitting_it_is_normal(self) -> None:
        # Most plans are self-contained. The pointer is for the ones that
        # localise a decision made once, somewhere else.
        assert parse(ONE_TASK).doc is None

    def test_it_must_be_a_string(self) -> None:
        with pytest.raises(GraphError) as caught:
            parse("doc = 3\n" + ONE_TASK)

        assert [issue.code for issue in caught.value.issues] == ["invalid-type"]

    def test_an_empty_pointer_is_no_pointer(self) -> None:
        # A blank reference renders a line telling the agent to read nothing,
        # which is worse than silence.
        with pytest.raises(GraphError) as caught:
            parse('doc = "   "\n' + ONE_TASK)

        assert [issue.code for issue in caught.value.issues] == ["invalid-type"]


class TestItReachesTheBody:
    def test_the_body_names_the_document_and_the_precedence(self) -> None:
        graph = parse('doc = "docs/design.md"\n' + ONE_TASK)

        body = build_body(graph.tasks[0], plan=graph.plan, doc=graph.doc)

        assert "docs/design.md" in body
        assert "this issue" in body.lower()

    def test_a_graph_without_one_renders_no_line(self) -> None:
        body = build_body(parse(ONE_TASK).tasks[0], plan="demo", doc=None)

        assert "background" not in body.lower()

    def test_the_pointer_sits_after_the_prose_and_before_the_block(self) -> None:
        # Order is the argument: the agent reads the brief, then learns the
        # brief outranks the document it is about to be sent to.
        graph = parse('doc = "docs/design.md"\n' + ONE_TASK)

        body = build_body(graph.tasks[0], plan=graph.plan, spec="The brief.", doc=graph.doc)

        assert body.index("The brief.") < body.index("docs/design.md")
        assert body.index("docs/design.md") < body.index("<!-- dispatchkit")

    def test_a_contradiction_is_reported_not_reconciled(self) -> None:
        graph = parse('doc = "docs/design.md"\n' + ONE_TASK)

        body = build_body(graph.tasks[0], plan=graph.plan, doc=graph.doc)

        assert "say different things" in body
