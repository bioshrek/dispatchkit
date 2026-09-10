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
