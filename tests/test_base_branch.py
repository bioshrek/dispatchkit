"""D16: a plan says which branch it integrates on.

`base` is a per-plan key. Omitted, it is `main`, which is what every plan does
today — so this is additive, and the trunk-based reading stays available to
anyone who wants it.

It is a value object rather than a `str` because of where the value goes. It
becomes part of an argv (`git fetch <remote> <base>`), and it becomes a GraphQL
variable handed to `replaceActorsForAssignable`. A branch name arrives from a
committed graph file, which is reviewed — but the fence exists precisely
because a `verify: auto` merge can rewrite a graph file unattended, so "it was
reviewed" is a weaker guarantee here than it looks.

The refusals that matter are the ones that stop being a branch name and start
being something else: a leading `-` is an option rather than a ref, and git's
own `check-ref-format` rules rule out the rest. Validating once, at the edge,
is what keeps every later use from having to ask.
"""

from __future__ import annotations

import pytest

from dispatchkit.errors import GraphError
from dispatchkit.model import Base, TaskGraph
from dispatchkit.parse import parse_graph

pytestmark = pytest.mark.unit

GRAPH = """
plan = "wordfreq"

[[task]]
id = "one"
title = "One"
milestone = "M1"
lane = "cloud"
acceptance = "make check"
"""


def parsed(text: str) -> TaskGraph:
    return parse_graph(text, plan="wordfreq")


def issues_of(text: str) -> list[str]:
    try:
        parsed(text)
    except GraphError as exc:
        return [issue.message for issue in exc.issues]
    return []


class TestTheDefault:
    def test_a_plan_without_a_base_integrates_on_main(self) -> None:
        assert parsed(GRAPH).base == Base("main")

    def test_the_default_is_not_a_special_case(self) -> None:
        # Nothing downstream should have to ask whether a base was written.
        assert parsed(GRAPH).base is not None


class TestAnExplicitBase:
    def test_it_is_read(self) -> None:
        assert parsed(f'base = "plan/wordfreq"\n{GRAPH}').base == Base("plan/wordfreq")

    def test_it_prints_as_the_branch_name(self) -> None:
        assert str(parsed(f'base = "plan/wordfreq"\n{GRAPH}').base) == "plan/wordfreq"


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "--upload-pack=touch /tmp/pwned",
            "-x",
            "has space",
            "a..b",
            "a~1",
            "a^",
            "a:b",
            "a?b",
            "a*b",
            "a[b",
            "a\\b",
            "/leading",
            "trailing/",
            "double//slash",
            "ends.lock",
            "ends.",
            ".starts",
            "with\nnewline",
            "with\x00null",
            "@",
            "a@{b",
        ],
    )
    def test_it_is_not_a_branch_name(self, value: str) -> None:
        assert Base.check(value) is not None, f"{value!r} was accepted"

    @pytest.mark.parametrize(
        "value",
        ["main", "plan/wordfreq", "release/2.x", "a", "feature/JIRA-123_thing", "v1.0"],
    )
    def test_it_is_a_branch_name(self, value: str) -> None:
        assert Base.check(value) is None, f"{value!r} was refused"


class TestTheGraphRejectsABadBase:
    def test_a_non_string_is_an_error(self) -> None:
        assert any("base" in message for message in issues_of(f"base = 3\n{GRAPH}"))

    def test_an_option_looking_base_is_an_error(self) -> None:
        assert any("base" in message for message in issues_of(f'base = "-x"\n{GRAPH}'))

    def test_the_error_names_the_key(self) -> None:
        messages = issues_of(f'base = "has space"\n{GRAPH}')
        assert messages and all("`base`" in message for message in messages)


class TestTheSchemaStaysClosed:
    def test_an_unknown_top_level_key_is_still_refused(self) -> None:
        assert any("unknown" in message for message in issues_of(f'bass = "x"\n{GRAPH}'))


class TestTheBaseReachesTheIssue:
    """Found live: `apply` created the branch and wrote `base: main` (D16).

    Every offline test read the base back out of a fixture built by
    `tests/items.py`, which renders its own block. Nothing exercised the path
    that actually matters -- graph file to issue body -- so a `base` that was
    parsed, validated and then dropped on the floor between `plan_apply` and
    `render_block` was invisible until an issue existed to read.

    The machine block is the only place a base can travel: `watch` is allowed
    to run with no checkout, so a base left in the graph file is a base the
    scheduler cannot see.
    """

    def test_the_body_carries_the_plans_base(self) -> None:
        from dispatchkit.apply import build_body
        from tests.graphs import task as make_task

        body = build_body(make_task("a"), plan="demo", base=Base("plan/demo"))
        assert "base: plan/demo" in body

    def test_a_plan_on_main_still_says_main(self) -> None:
        from dispatchkit.apply import build_body
        from tests.graphs import task as make_task

        assert "base: main" in build_body(make_task("a"), plan="demo")

    def test_it_survives_the_whole_of_apply(self) -> None:
        from dispatchkit.apply import plan_apply
        from dispatchkit.github import CreateIssue, RepoState
        from tests.graphs import graph
        from tests.graphs import task as make_task

        plan = plan_apply(graph(make_task("a"), base=Base("plan/demo")), RepoState(()))
        created = [op for op in plan.operations if isinstance(op, CreateIssue)]
        assert created and "base: plan/demo" in created[0].body

    def test_and_reads_back_as_the_base_the_scheduler_uses(self) -> None:
        from dispatchkit.apply import plan_apply
        from dispatchkit.github import CreateIssue, IssueState, RepoState
        from dispatchkit.resolve import build_items
        from tests.graphs import graph
        from tests.graphs import task as make_task

        plan = plan_apply(graph(make_task("a"), base=Base("plan/demo")), RepoState(()))
        created = next(op for op in plan.operations if isinstance(op, CreateIssue))
        items, _ = build_items(
            RepoState(
                (
                    IssueState(
                        number=1,
                        title=created.title,
                        body=created.body,
                        labels=created.labels,
                        closed=False,
                    ),
                )
            )
        )
        assert [str(item.base) for item in items] == ["plan/demo"]
