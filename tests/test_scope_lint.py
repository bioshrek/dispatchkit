"""D9.2: the plan states its scope twice, so the two copies must agree.

`document-flags` declared `touches = ["README.md"]` and an acceptance of
`uv run pytest -q -k readme`. Those cannot both be right: an acceptance that
runs a test suite is satisfied by a test file, and no test file was in scope.
The plan contradicted itself and nothing checked.

This is cheap because both halves are already in the graph, and it is a
warning rather than an error for the usual D2 reason -- the human ratifies the
decomposition anyway, and `validate_graph` remains the only thing that stops a
push. It is also, deliberately, the *only* remaining consequence of a narrow
`touches`: since D9.2 the declaration no longer gates a merge, so the moment
to question it is when it is written, once, rather than in every pass for ever.
"""

from __future__ import annotations

import pytest

from dispatchkit.lints import lint_graph

from .graphs import graph, task

pytestmark = pytest.mark.unit


def codes(*tasks: object) -> list[str]:
    return [issue.code for issue in lint_graph(graph(*tasks))]  # type: ignore[arg-type]


class TestAnAcceptanceThatRunsTests:
    def test_the_live_case_is_caught(self) -> None:
        assert "scope-omits-tests" in codes(
            task("a", acceptance="uv run pytest -q -k readme", touches=("README.md",)),
            task("b", depends=("a",)),
            task("c", depends=("a",)),
        )

    def test_a_declared_test_path_is_clean(self) -> None:
        assert "scope-omits-tests" not in codes(
            task(
                "a",
                acceptance="uv run pytest -q -k readme",
                touches=("README.md", "tests/test_readme.py"),
            ),
            task("b", depends=("a",)),
            task("c", depends=("a",)),
        )

    def test_a_glob_over_the_test_tree_is_clean(self) -> None:
        assert "scope-omits-tests" not in codes(
            task("a", acceptance="pytest", touches=("src/**", "tests/**")),
            task("b", depends=("a",)),
            task("c", depends=("a",)),
        )

    def test_a_spec_directory_counts_as_tests(self) -> None:
        # The convention is not universal, and a lint that only knows one
        # ecosystem's spelling reports its own ignorance as a defect.
        assert "scope-omits-tests" not in codes(
            task("a", acceptance="npm test", touches=("src/**", "spec/**")),
            task("b", depends=("a",)),
            task("c", depends=("a",)),
        )

    def test_a_wide_scope_is_clean(self) -> None:
        # `**` admits the test file along with everything else. There is
        # nothing to warn about, whatever one thinks of the declaration.
        assert "scope-omits-tests" not in codes(
            task("a", acceptance="go test ./...", touches=("**",)),
            task("b", depends=("a",)),
            task("c", depends=("a",)),
        )

    def test_other_runners_are_recognised(self) -> None:
        for acceptance in ("cargo test", "go test ./...", "npm test", "mix test", "vitest run"):
            assert "scope-omits-tests" in codes(
                task("a", acceptance=acceptance, touches=("src/a.py",)),
                task("b", depends=("a",)),
                task("c", depends=("a",)),
            ), acceptance


class TestWhenItStaysQuiet:
    def test_an_acceptance_that_runs_no_tests_says_nothing(self) -> None:
        assert "scope-omits-tests" not in codes(
            task("a", acceptance="./build.sh", touches=("README.md",)),
            task("b", depends=("a",)),
            task("c", depends=("a",)),
        )

    def test_an_undeclared_scope_says_nothing(self) -> None:
        # There is no declaration to disagree with the acceptance. Since D9.2
        # an empty `touches` costs only a weaker exclusion, and demanding one
        # from every task is a different lint nobody asked for.
        assert "scope-omits-tests" not in codes(
            task("a", acceptance="pytest", touches=()),
            task("b", depends=("a",)),
            task("c", depends=("a",)),
        )

    def test_the_word_test_inside_another_word_is_not_a_runner(self) -> None:
        # `latest`, `contest`, `attestation`. Matching a bare substring would
        # make the lint fire on commands that run nothing of the kind.
        assert "scope-omits-tests" not in codes(
            task("a", acceptance="./scripts/attest-latest.sh", touches=("src/a.py",)),
            task("b", depends=("a",)),
            task("c", depends=("a",)),
        )


class TestTheMessage:
    def test_it_names_both_halves_of_the_contradiction(self) -> None:
        issues = lint_graph(
            graph(
                task("a", acceptance="uv run pytest -q -k readme", touches=("README.md",)),
                task("b", depends=("a",)),
                task("c", depends=("a",)),
            )
        )
        message = next(issue.message for issue in issues if issue.code == "scope-omits-tests")
        assert "pytest" in message
        assert "touches" in message

    def test_it_is_reported_against_the_task(self) -> None:
        issues = lint_graph(
            graph(
                task("a", acceptance="pytest", touches=("README.md",)),
                task("b", depends=("a",)),
                task("c", depends=("a",)),
            )
        )
        where = next(issue.where for issue in issues if issue.code == "scope-omits-tests")
        assert "a" in where
