"""D16: `doctor` checks the branch auto-merge actually targets.

The merge gate was anchored to `main` when `main` was the only place a
`verify: auto` merge could land. It is not any more: a plan integrates on its
own branch, and that is where task pull requests are merged unattended.

Left as it was, the check reports on a branch nothing merges into and stays
silent about the one that does — and it fails in the reassuring direction. A
repository with a protected `main` and an unprotected `plan/wordfreq` would be
told it has two locks on a merge that has one. A diagnostic that is confidently
wrong is worse than a missing one, because the whole reason this check exists
is that dispatchkit's own reading of CI might be the only thing standing
between an agent and the tree.

The plan bases are read from the plan files rather than the repository, because
they are declared there: a base that no plan names is not a branch anything
will merge into, and a base named by a plan is one even if it has yet to be
created.
"""

from __future__ import annotations

import pytest

from dispatchkit.doctor import Diagnostics, check_remote

pytestmark = pytest.mark.unit


def gate(**kwargs: object) -> str:
    diagnostics = Diagnostics(scopes=("repo",), agent_available=True, **kwargs)  # type: ignore[arg-type]
    checks = {check.name: check for check in check_remote(diagnostics)}
    check = checks["merge-gate"]
    return f"{'ok' if check.ok else 'bad'}: {check.detail}"


class TestTheBranchesItReportsOn:
    def test_an_unprotected_plan_branch_is_named(self) -> None:
        verdict = gate(unprotected_bases=("plan/wordfreq",))
        assert verdict.startswith("bad")
        assert "plan/wordfreq" in verdict

    def test_every_base_protected_passes(self) -> None:
        assert gate(unprotected_bases=()).startswith("ok")

    def test_the_default_branch_is_no_longer_the_question(self) -> None:
        # The reassuring failure this replaces: a protected `main` beside a
        # bare `plan/wordfreq` used to read green. There is now no way to say
        # "main is protected" to this check at all, because it is not what a
        # `verify: auto` merge lands on.
        assert not hasattr(Diagnostics(scopes=(), agent_available=False), "protected_branch")

    def test_several_are_all_named(self) -> None:
        verdict = gate(unprotected_bases=("plan/a", "plan/b"))
        assert "plan/a" in verdict and "plan/b" in verdict


class TestTheRemedyIsActionable:
    def test_it_says_what_to_protect(self) -> None:
        diagnostics = Diagnostics(
            scopes=("repo",), agent_available=True, unprotected_bases=("plan/wordfreq",)
        )
        check = next(c for c in check_remote(diagnostics) if c.name == "merge-gate")
        assert check.remedy and "plan/wordfreq" in check.remedy


class TestReadingTheBases:
    """Which branches the CLI asks about."""

    def test_the_bases_come_from_the_plans(self, tmp_path: object) -> None:
        from pathlib import Path

        from dispatchkit.cli import declared_bases

        plans = Path(str(tmp_path))
        (plans / "wordfreq.tasks.toml").write_text(
            'plan = "wordfreq"\nbase = "plan/wordfreq"\n\n'
            '[[task]]\nid = "a"\ntitle = "a"\nmilestone = "M"\n'
            'lane = "cloud"\nverify = "human"\nacceptance = "true"\n',
            encoding="utf-8",
        )
        assert declared_bases(plans) == ("plan/wordfreq",)

    def test_a_plan_with_no_base_asks_about_main(self, tmp_path: object) -> None:
        from pathlib import Path

        from dispatchkit.cli import declared_bases

        plans = Path(str(tmp_path))
        (plans / "demo.tasks.toml").write_text(
            'plan = "demo"\n\n[[task]]\nid = "a"\ntitle = "a"\n'
            'milestone = "M"\nlane = "cloud"\nverify = "human"\nacceptance = "true"\n',
            encoding="utf-8",
        )
        assert declared_bases(plans) == ("main",)

    def test_an_unreadable_plan_is_skipped_rather_than_fatal(self, tmp_path: object) -> None:
        from pathlib import Path

        from dispatchkit.cli import declared_bases

        # `doctor` is the command people run when things are broken. A plan it
        # cannot parse is reported by `validate`, and taking the diagnostic
        # down with it would withhold every other answer.
        plans = Path(str(tmp_path))
        (plans / "broken.tasks.toml").write_text("this is not toml {{{", encoding="utf-8")
        assert declared_bases(plans) == ()

    def test_a_missing_plans_directory_is_empty(self, tmp_path: object) -> None:
        from pathlib import Path

        from dispatchkit.cli import declared_bases

        assert declared_bases(Path(str(tmp_path)) / "nope") == ()
