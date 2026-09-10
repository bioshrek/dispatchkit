"""D6.6: a local run that raises took the whole thing down quietly.

The fifth live run ended like this:

    Exception in thread dispatchkit-local:
    ...
    RuntimeError: gh pr failed: ... No commits between main and ...
      wordfreq/version-flag       Dispatched
    local: wordfreq/version-flag did not report
    pass complete: 1 dispatched
    exit=0

Three separate things are wrong with that ending. The traceback went to stderr
where nothing collects it. `self.last` was never assigned, so the pass could
only say "did not report" — the least informative thing it knows how to say,
for a failure it had the whole reason for. And the pass exited 0, so a caller,
a cron entry or a CI step would all read that run as fine.

Underneath, the mark stayed on. `run_local` releases it in `_finish`, and an
exception goes around `_finish`, so the issue stayed claimed with nothing
running it — the D6.0 bug, arrived at from a direction D6.0 did not cover.

`run_local` already treats every *machine* failure as a value: a failed
worktree, agent, acceptance or push all come back as a `LocalRun`. It is only
the `GitHubApi` calls that raise, and `gh_cli` raises `RuntimeError` for any
non-zero `gh`. So the rule is the one already in use, extended to the half of
the run that was missing it: stop at the first failure, report it, release.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.dispatcher import LocalDispatcher
from dispatchkit.github import LABEL_LOCAL_CLAIM
from dispatchkit.local import run_local
from dispatchkit.model import Lane, TaskId
from dispatchkit.resolve import TaskItem, build_items
from tests.fake_github import FakeGitHub
from tests.fake_workstation import FakeWorkstation
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
ROOT = Path("/tmp/dispatchkit-test")
WORKTREE = ROOT / "demo" / "version-flag"


class Exploding(FakeGitHub):
    """A `gh` that fails the way `gh_cli` fails: `RuntimeError`."""

    def __init__(self, *args: object, failing: str = "open_pr", **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.failing = failing

    def open_pr(self, **kwargs: object) -> int:
        if self.failing == "open_pr":
            raise RuntimeError("gh pr failed: No commits between main and dispatchkit/x/y/3")
        return super().open_pr(**kwargs)  # type: ignore[arg-type]


def task_of(api: FakeGitHub) -> TaskItem:
    resolved, _ = build_items(api.fetch_state())
    return next(item for item in resolved if item.ref.id == TaskId("version-flag"))


def api_with_claim() -> Exploding:
    return Exploding(
        state=state_of(
            issue(
                "version-flag",
                number=1,
                lane=Lane.LOCAL,
                labels=("dispatchkit", LABEL_LOCAL_CLAIM),
            )
        )
    )


def machine() -> FakeWorkstation:
    return FakeWorkstation(commit_counts={WORKTREE: 1})


class TestTheRunReportsInsteadOfRaising:
    def test_it_does_not_propagate(self) -> None:
        run = run_local(
            task_of(api_with_claim()),
            api=api_with_claim(),
            machine=machine(),
            config=SchedulerConfig(),
            root=ROOT,
            now=NOW,
        )

        assert not run.ok

    def test_the_reason_survives(self) -> None:
        run = run_local(
            task_of(api_with_claim()),
            api=api_with_claim(),
            machine=machine(),
            config=SchedulerConfig(),
            root=ROOT,
            now=NOW,
        )

        assert "No commits between" in run.detail

    def test_the_stage_is_named(self) -> None:
        run = run_local(
            task_of(api_with_claim()),
            api=api_with_claim(),
            machine=machine(),
            config=SchedulerConfig(),
            root=ROOT,
            now=NOW,
        )

        assert run.stage == "report"

    def test_the_mark_comes_off(self) -> None:
        api = api_with_claim()

        run_local(
            task_of(api),
            api=api,
            machine=machine(),
            config=SchedulerConfig(),
            root=ROOT,
            now=NOW,
        )

        assert any(
            call.startswith("edit_labels") and LABEL_LOCAL_CLAIM in call for call in api.calls
        ), api.calls


class TestTheDispatcherSurvivesAnythingElse:
    """The last resort. Whatever else breaks, the thread must not die mute."""

    def test_a_raising_run_still_reports(self) -> None:
        api = api_with_claim()
        items, _ = build_items(api.fetch_state())
        # `worktrees()` is called by `_clear`, before the run proper.
        class Angry(FakeWorkstation):
            def worktrees(self) -> tuple[()]:
                raise OSError("the disk went away")

        dispatcher = LocalDispatcher(
            api=api,
            machine=Angry(),
            config=SchedulerConfig(),
            root=ROOT,
            spawn=lambda fn: _Now(fn),
        )
        dispatcher.serve(items, now=NOW, marked=())
        dispatcher.wait()

        assert dispatcher.last is not None
        assert not dispatcher.last.ok
        assert "disk went away" in dispatcher.last.detail


class _Now:
    """Runs the work on the spot, so the test needs no thread."""

    def __init__(self, fn: object) -> None:
        self.done = False
        assert callable(fn)
        fn()
        self.done = True

    def is_alive(self) -> bool:
        return False

    def wait(self) -> None:
        return None
