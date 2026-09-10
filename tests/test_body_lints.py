"""D10: the issue body is a contract, and an unwritten one is a warning.

`build_body` falls back to the task's title when there is no `body_file`, so a
task can reach an agent as a heading and an acceptance command and nothing
else. Nothing objected, and the sandbox bodies came out thin exactly as the
design predicted they would: "the sandbox bodies came out thin because nothing
objected".

The consequence is not cosmetic. The prompt *is* the body's prose -- the
machine block is cut out and `touches` is deliberately withheld -- so a task
with no prose dispatches an agent with the title, the acceptance, and no
statement of what done looks like. The retry budget then spends three attempts
discovering it.

Warnings, never errors, like every other lint here: `validate_graph` remains
the only thing that can stop a push, and a one-line task is a legitimate thing
to write on purpose.

Bodies live in separate files, so `lint_graph` cannot read them and must be
handed them. `specs=None` means nobody looked, and the body lints stay silent
rather than reporting every task as unwritten; an empty mapping means somebody
looked and found nothing.
"""

from __future__ import annotations

import re
import time

import pytest

from dispatchkit.lints import _paths_in, lint_graph
from dispatchkit.model import Task, TaskId

from .graphs import graph, task

pytestmark = pytest.mark.unit


def codes(*tasks: Task, specs: dict[TaskId, str] | None = None) -> list[str]:
    return [issue.code for issue in lint_graph(graph(*tasks), specs=specs)]


def spread() -> tuple[Task, Task]:
    """Two extra tasks, so the shape lints have nothing to say."""
    return task("b", depends=("a",)), task("c", depends=("a",))


class TestATaskWithNoBody:
    def test_a_task_with_no_body_file_is_flagged(self) -> None:
        assert "thin-body" in codes(task("a"), *spread(), specs={})

    def test_a_task_with_a_body_is_clean(self) -> None:
        written = dict.fromkeys(
            (TaskId("a"), TaskId("b"), TaskId("c")), "Do the thing, and here is how."
        )
        assert "thin-body" not in codes(task("a"), *spread(), specs=written)

    def test_only_the_unwritten_task_is_named(self) -> None:
        issues = lint_graph(
            graph(task("a"), *spread()),
            specs={TaskId("b"): "Written.", TaskId("c"): "Written too."},
        )
        assert [issue.where for issue in issues if issue.code == "thin-body"] == ["a"]

    def test_a_blank_body_file_is_no_body_at_all(self) -> None:
        # `build_body` treats whitespace as absent and falls back to the
        # title, so the lint has to agree with it or it would pass a task
        # whose issue says nothing.
        assert "thin-body" in codes(task("a"), *spread(), specs={TaskId("a"): "   \n\n  "})

    def test_nothing_is_said_when_nobody_read_the_bodies(self) -> None:
        # `specs=None` is "not checked". Reporting every task as unwritten
        # because the caller did not open the files would make the lint fire
        # loudest where it knows least.
        assert "thin-body" not in codes(task("a"), *spread())

    def test_it_names_the_task_and_the_key(self) -> None:
        issues = lint_graph(graph(task("a"), *spread()), specs={})
        message = next(issue.message for issue in issues if issue.code == "thin-body")
        assert "body_file" in message


class TestAPathNamedInProseButNotInScope:
    """The third place a plan states its scope, and the one nothing checked.

    `document-flags` said "Add `tests/test_readme.py`" in its prose while its
    `touches` admitted only `README.md`. `scope-omits-tests` catches that from
    the acceptance; this catches it from the brief, which is the copy the
    agent actually reads and therefore the one it obeys.
    """

    def test_a_path_the_brief_names_is_expected_in_touches(self) -> None:
        assert "scope-omits-named-path" in codes(
            task("a", touches=("README.md",)),
            *spread(),
            specs={TaskId("a"): "Add `tests/test_readme.py` asserting the flags are documented."},
        )

    def test_a_declared_path_is_clean(self) -> None:
        assert "scope-omits-named-path" not in codes(
            task("a", touches=("README.md", "tests/**")),
            *spread(),
            specs={TaskId("a"): "Add `tests/test_readme.py`."},
        )

    def test_an_undeclared_scope_says_nothing(self) -> None:
        assert "scope-omits-named-path" not in codes(
            task("a", touches=()),
            *spread(),
            specs={TaskId("a"): "Add `tests/test_readme.py`."},
        )

    def test_a_flag_is_not_a_path(self) -> None:
        # Bodies are full of backticked things that are not files. Requiring
        # a slash is the cheapest rule that admits no flag, no command and no
        # version string, and it still catches the case that started this.
        assert "scope-omits-named-path" not in codes(
            task("a", touches=("README.md",)),
            *spread(),
            specs={TaskId("a"): "Document `--top`, `--stopwords` and `--json`, each briefly."},
        )

    def test_a_command_is_not_a_path(self) -> None:
        assert "scope-omits-named-path" not in codes(
            task("a", touches=("README.md",)),
            *spread(),
            specs={TaskId("a"): "The acceptance is `uv run pytest -q -k readme`."},
        )

    def test_a_url_is_not_a_path(self) -> None:
        assert "scope-omits-named-path" not in codes(
            task("a", touches=("README.md",)),
            *spread(),
            specs={TaskId("a"): "See `https://example.com/spec` for the format."},
        )

    def test_a_fenced_block_is_not_read_for_paths(self) -> None:
        # A worked example in a code fence is illustration, not scope, and
        # fences are where commands and diffs live.
        assert "scope-omits-named-path" not in codes(
            task("a", touches=("README.md",)),
            *spread(),
            specs={TaskId("a"): "Like so:\n\n```sh\ncat src/app/ports.py\n```\n"},
        )

    def test_every_undeclared_path_is_named_once(self) -> None:
        issues = lint_graph(
            graph(task("a", touches=("README.md",)), *spread()),
            specs={TaskId("a"): "Edit `src/a.py` and `src/b.py`, and again `src/a.py`."},
        )
        message = next(
            issue.message for issue in issues if issue.code == "scope-omits-named-path"
        )
        assert message.count("src/a.py") == 1
        assert "src/b.py" in message


class TestTheFenceIsStrippedHowever:
    """A fence is illustration, and the three ways a real brief writes one all
    have to count. Only the tidiest was handled: a paired, unindented fence.

    The indented case is the one that bites. A code block under a numbered
    step is ordinary markdown, and it is exactly the shape the body contract's
    "definition of done" section invites -- so a brief could fail
    `validate --strict` for a path its task never touches.
    """

    def test_a_plain_fence_is_illustration(self) -> None:
        body = "Change `src/a.py`.\n\n```sh\ncat `docs/notes.md`\n```\n"

        assert codes(task("a", touches=("src/a.py",)), *spread(), specs=written(body)) == []

    def test_an_indented_fence_is_illustration_too(self) -> None:
        body = "1. Run it:\n\n   ```sh\n   see `docs/notes.md`\n   ```\n"

        assert codes(task("a", touches=("src/a.py",)), *spread(), specs=written(body)) == []

    def test_an_unterminated_fence_runs_to_the_end(self) -> None:
        # A dropped closing fence is a typo in the brief, not a licence to
        # start reading a code block as prose.
        body = "Change `src/a.py`.\n\n```sh\ncat `docs/notes.md`\n"

        assert codes(task("a", touches=("src/a.py",)), *spread(), specs=written(body)) == []

    def test_prose_after_a_closed_fence_is_still_prose(self) -> None:
        # The fix must not swallow the rest of the document.
        body = "```sh\necho hi\n```\n\nThen edit `docs/notes.md`.\n"

        assert codes(task("a", touches=("src/a.py",)), *spread(), specs=written(body)) == [
            "scope-omits-named-path"
        ]

    def test_two_fences_do_not_swallow_the_prose_between_them(self) -> None:
        body = "```sh\na\n```\n\nEdit `docs/notes.md`.\n\n```sh\nb\n```\n"

        assert codes(task("a", touches=("src/a.py",)), *spread(), specs=written(body)) == [
            "scope-omits-named-path"
        ]


def written(body: str) -> dict[TaskId, str]:
    """`a` gets the body under test; the two shape-lint fillers get a plain one."""
    return {TaskId("a"): body, TaskId("b"): "Written.", TaskId("c"): "Written too."}


class TestThePathPatternIsLinear:
    """The old pattern was `[^`\\s]+/[^`\\s]+` -- two greedy runs both
    admitting a slash, which backtracks quadratically on an unterminated
    backtick followed by a long run of them. Not attacker-reachable, since a
    `body_file` is local rather than an issue body, but the rewrite has to be
    proven equivalent rather than assumed to be."""

    def test_it_agrees_with_the_pattern_it_replaced(self) -> None:
        old = re.compile(r"`([^`\s]+/[^`\s]+)`")
        corpus = [
            "edit `src/a.py` then `tests/b.py`",
            "run `uv run pytest -q` and pass `--top`",
            "see `https://example.com/x` for `v1.2/3`",
            "`/etc/passwd` and `a/` and `a/b/` and `/a`",
            "no backticks at all, src/a.py bare",
            "`a` `b/c` `` `d/e`",
            "unterminated `a/b/c/d",
        ]

        for text in corpus:
            assert _paths_in(text) == list(dict.fromkeys(old.findall(text))), text

    def test_it_does_not_blow_up_on_a_long_unterminated_run(self) -> None:
        started = time.monotonic()

        _paths_in("`" + "a/" * 20000)

        assert time.monotonic() - started < 1.0
