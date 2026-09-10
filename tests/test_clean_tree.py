"""D13.1d: `verify: auto` requires a clean graph file.

The single review gate was a human ratifying `lane` and `verify` in a pull
request before anything reached GitHub. Apply-on-save deletes that gate, and
this is its replacement — the cheapest honest one available, because it is
already in the repository: if the graph file is dirty in the working tree, the
tasks it describes fall back to `verify: human` for the pass.

Merge authority is the one thing dispatchkit hands out that changes `main`
without a person, so it must not be grantable from an unsaved experiment. The
whole gate costs one `git status`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import IssueState
from dispatchkit.model import Checks, Lane, PullRequest, Verify
from dispatchkit.resolve import TaskItem, build_items, merge_ops
from dispatchkit.tick import plan_tick
from dispatchkit.workstation import dirty_plans
from tests.fake_github import FakeGitHub
from tests.items import issue, ref, state_of

pytestmark = pytest.mark.unit

CONFIG = SchedulerConfig()


def mergeable(name: str, number: int, pr: int, plan: str = "demo") -> IssueState:
    """An issue whose pull request clears every other merge gate, so the only
    thing that can stop it is the one under test."""
    return issue(
        name,
        number=number,
        plan=plan,
        verify=Verify.AUTO,
        touches=("src/*",),
        open_prs=(green(pr),),
    )


def green(number: int = 40) -> PullRequest:
    return PullRequest(
        number=number,
        draft=False,
        mergeable=True,
        checks=Checks.PASSING,
        files=("src/a.py",),
    )


def items_of(*issues: IssueState) -> tuple[TaskItem, ...]:
    built, _ = build_items(FakeGitHub(state_of(*issues)).fetch_state())
    return built


class TestReadingWhichPlansAreDirty:
    """The pure half: `git status --porcelain` in, plan names out.

    Only the graph files matter. A dirty `src/` is the ordinary condition of a
    developer's afternoon and says nothing about whether a `verify` value has
    been reviewed.
    """

    def test_a_modified_graph_file_names_its_plan(self) -> None:
        assert dirty_plans(" M docs/plans/wordfreq.toml\n", Path("docs/plans")) == frozenset(
            {"wordfreq"}
        )

    def test_an_untracked_graph_file_counts_too(self) -> None:
        # A plan that has never been committed has never been reviewed, which
        # is the strongest form of the thing this refuses.
        assert dirty_plans("?? docs/plans/new.toml\n", Path("docs/plans")) == frozenset({"new"})

    def test_a_staged_change_still_counts(self) -> None:
        # Staged is not committed, and the gate is about what a reviewer could
        # have seen, not about what git calls it.
        assert dirty_plans("M  docs/plans/wordfreq.toml\n", Path("docs/plans")) == frozenset(
            {"wordfreq"}
        )

    def test_a_dirty_source_file_is_not_a_dirty_plan(self) -> None:
        assert dirty_plans(" M src/tokenise.py\n", Path("docs/plans")) == frozenset()

    def test_a_rename_dirties_both_names(self) -> None:
        # `git status` reports `R  old -> new`, and both ends changed: one plan
        # gained a file and the other lost its only one. A gate fails closed,
        # so both are withheld rather than reasoning about which mattered.
        status = "R  docs/plans/old.toml -> docs/plans/wordfreq.toml\n"
        assert dirty_plans(status, Path("docs/plans")) == frozenset({"old", "wordfreq"})

    def test_a_clean_tree_dirties_nothing(self) -> None:
        assert dirty_plans("", Path("docs/plans")) == frozenset()

    def test_a_path_with_a_space_survives_the_quoting(self) -> None:
        # `git status --porcelain` quotes such a path. Getting this wrong would
        # silently *fail open*, which is the wrong direction for a gate.
        status = '?? "docs/plans/my plan.toml"\n'
        assert dirty_plans(status, Path("docs/plans")) == frozenset({"my plan"})


class TestTheGateItself:
    def test_a_clean_plan_merges_as_before(self) -> None:
        items = items_of(mergeable("a", 1, 40))
        assert len(merge_ops(items, CONFIG)) == 1

    def test_a_dirty_plan_does_not_merge(self) -> None:
        items = items_of(mergeable("a", 1, 40))
        assert merge_ops(items, CONFIG, dirty=frozenset({"demo"})) == ()

    def test_only_the_dirty_plan_is_degraded(self) -> None:
        # Plans are independent. One unsaved experiment must not stop every
        # other plan in the repository from merging.
        items = items_of(
            mergeable("a", 1, 40),
            mergeable("b", 2, 41, plan="other"),
        )
        merged = merge_ops(items, CONFIG, dirty=frozenset({"demo"}))
        assert [operation.ref for operation in merged] == [ref("b", plan="other")]

    def test_the_task_is_degraded_and_not_refused(self) -> None:
        # It falls back to `human`, so a person can still merge it. Nothing is
        # blocked; only the automatic authority is withheld.
        items = items_of(mergeable("a", 1, 40))
        assert merge_ops(items, CONFIG, dirty=frozenset({"demo"})) == ()

    def test_dispatch_is_untouched_by_a_dirty_plan(self) -> None:
        # The gate is about merge authority. Refusing to dispatch would make
        # editing a graph stop the work already described by it, which is the
        # opposite of what a graph file is for.
        state = state_of(issue("a", number=1, lane=Lane.CLOUD, verify=Verify.AUTO))
        plan = plan_tick(state, config=CONFIG, now=_now(), dirty=frozenset({"demo"}))
        assert plan.operations


class TestItSaysWhySomethingDidNotMerge:
    def test_a_degraded_task_is_reported(self) -> None:
        # A pull request sitting green and unmerged with no explanation is the
        # failure mode this whole notice family exists to prevent.
        state = state_of(mergeable("a", 1, 40))
        plan = plan_tick(state, config=CONFIG, now=_now(), dirty=frozenset({"demo"}))
        assert any("dirty" in str(notice) for notice in plan.notices)

    def test_a_clean_plan_says_nothing(self) -> None:
        state = state_of(mergeable("a", 1, 40))
        plan = plan_tick(state, config=CONFIG, now=_now())
        assert not any("dirty" in str(notice) for notice in plan.notices)

    def test_a_dirty_plan_with_nothing_to_merge_says_nothing(self) -> None:
        # Editing a graph is normal. Saying so on every pass would train the
        # reader to skip the line that matters.
        state = state_of(issue("a", number=1, verify=Verify.AUTO))
        plan = plan_tick(state, config=CONFIG, now=_now(), dirty=frozenset({"demo"}))
        assert not any("dirty" in str(notice) for notice in plan.notices)


def _now() -> datetime:
    return datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class TestTheCommandThatAsks:
    def test_status_is_asked_for_the_plans_directory_only(self) -> None:
        # Narrowing at the git end rather than filtering everything afterwards:
        # in a repository mid-refactor the untracked list can be thousands of
        # lines, and none of them can answer this question.
        from dispatchkit.workstation_cli import status_command

        assert status_command(Path("docs/plans")) == [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "docs/plans",
        ]

    def test_it_is_an_argv_list_with_no_shell_in_it(self) -> None:
        from dispatchkit.workstation_cli import status_command

        command = status_command(Path("docs/plans"))
        assert command[0] == "git"
        assert "-c" not in command

    def test_a_repository_git_cannot_read_dirties_nothing(self) -> None:
        # Failing closed here would refuse every auto-merge on any machine
        # where `watch` runs outside a checkout, which is a legitimate way to
        # use it: the graph lives on GitHub too.
        from dispatchkit.workstation import RunResult
        from dispatchkit.workstation_cli import CliWorkstation

        class Broken(CliWorkstation):
            def _git(self, command, *, cwd=None):  # type: ignore[no-untyped-def]
                return RunResult(tuple(command), 128, "not a git repository")

        assert Broken(root=Path("/work")).dirty(plans=Path("docs/plans")) == frozenset()


class TestTheGateIsOnByDefault:
    """The whole point is that it needs no flag. A replacement for a review
    gate that has to be switched on is not a replacement for anything."""

    def test_a_real_pass_asks_git(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import dispatchkit.cli as cli

        asked: list[Path] = []

        class Watching:
            def dirty(self, *, plans: Path) -> frozenset[str]:
                asked.append(plans)
                return frozenset({"demo"})

        monkeypatch.setattr(cli, "CliWorkstation", lambda **_: Watching())
        fixture = Path(__file__).resolve().parent / "fixtures" / "search_issues.json"
        code = cli.main(
            ["watch", "--once", "--state", str(fixture), "--config", str(tmp_path / "none.toml")]
        )
        assert code == 0
        assert asked == [CONFIG.plans]
