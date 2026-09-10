"""D6.5: the executor, wired into `watch` without holding it up.

Two components in one process. The scheduler marks `dispatch:local` and moves
on; the dispatcher discovers work by reading issue state, exactly as a cloud
agent does. They share no queue and no memory, so a two-hour local task cannot
stop a pass from merging a cloud one, and the dispatcher never holds anything
the next pass could not recompute.

That independence is what makes the threading safe rather than merely
convenient: the thread is a *worker*, not a place decisions live. It is asked
one question — "is it still going?" — and everything else is re-derived from
GitHub every pass.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.dispatcher import LocalDispatcher, Running
from dispatchkit.github import LABEL_LOCAL_CLAIM, IssueState, RepoState
from dispatchkit.model import Lane, TaskRef
from dispatchkit.resolve import TaskItem, build_items
from dispatchkit.workstation import Worktree
from tests.fake_github import FakeGitHub
from tests.fake_workstation import ROOT, FakeWorkstation
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
CONFIG = SchedulerConfig()
MARKED = ("dispatchkit", LABEL_LOCAL_CLAIM)


def here(target: Callable[[], None]) -> Running:
    """Run the 'thread' inline, so a test sees the whole run and no clock."""

    class Immediate:
        def __init__(self) -> None:
            target()

        def is_alive(self) -> bool:
            return False

    return Immediate()


class Never:
    """A 'thread' that starts and never finishes, for the busy case."""

    def __init__(self, target: Callable[[], None]) -> None:
        self.target = target

    def is_alive(self) -> bool:
        return True


def dispatcher(
    api: FakeGitHub,
    machine: FakeWorkstation,
    spawn: Callable[[Callable[[], None]], Running] = here,
) -> LocalDispatcher:
    return LocalDispatcher(api=api, machine=machine, config=CONFIG, root=ROOT, spawn=spawn)


def items(api: FakeGitHub) -> Sequence[TaskItem]:
    resolved, _ = build_items(api.fetch_state())
    return resolved


class TestPickingUpWork:
    def test_a_marked_local_task_is_run(self) -> None:
        api = FakeGitHub(_state(issue("gpu", number=1, lane=Lane.LOCAL, labels=MARKED)))
        machine = FakeWorkstation()
        dispatcher(api, machine).serve(items(api), now=NOW)
        assert machine.created == [ROOT / "demo" / "gpu"]

    def test_an_unmarked_task_is_left_for_the_scheduler(self) -> None:
        # The mark is the handoff. Running an unmarked task would race the
        # pass that was about to mark it, and both would run it.
        api = FakeGitHub(_state(issue("gpu", number=1, lane=Lane.LOCAL)))
        machine = FakeWorkstation()
        dispatcher(api, machine).serve(items(api), now=NOW)
        assert machine.created == []

    def test_a_cloud_task_is_never_run_here(self) -> None:
        # It cannot be marked in the ordinary course of things, but a human
        # can apply a label, and "somebody typed it" is not authority to run
        # arbitrary work on this machine.
        api = FakeGitHub(_state(issue("gpu", number=1, lane=Lane.CLOUD, labels=MARKED)))
        machine = FakeWorkstation()
        dispatcher(api, machine).serve(items(api), now=NOW)
        assert machine.created == []

    def test_a_closed_task_is_not_run(self) -> None:
        api = FakeGitHub(
            _state(issue("gpu", number=1, lane=Lane.LOCAL, closed=True, labels=MARKED))
        )
        machine = FakeWorkstation()
        dispatcher(api, machine).serve(items(api), now=NOW)
        assert machine.created == []

    def test_only_one_runs_at_a_time(self) -> None:
        # `caps.local = 1` is a rule about this machine, not a scheduling
        # preference, so it is enforced here as well as in admission.
        api = FakeGitHub(
            _state(
                issue("gpu", number=1, lane=Lane.LOCAL, labels=MARKED),
                issue("cuda", number=2, lane=Lane.LOCAL, labels=MARKED),
            )
        )
        machine = FakeWorkstation()
        dispatcher(api, machine).serve(items(api), now=NOW)
        assert len(machine.created) == 1

    def test_a_busy_dispatcher_starts_nothing(self) -> None:
        api = FakeGitHub(_state(issue("gpu", number=1, lane=Lane.LOCAL, labels=MARKED)))
        machine = FakeWorkstation()
        served = dispatcher(api, machine, spawn=Never)
        served.serve(items(api), now=NOW)
        served.serve(items(api), now=NOW)
        assert machine.created == []
        assert served.busy


class TestStartingFromWhateverWasLeft:
    def test_startup_releases_a_mark_it_cannot_account_for(self) -> None:
        # Before anything is running, every mark on the repository is by
        # definition abandoned: this is the only dispatcher, and it has just
        # started.
        api = FakeGitHub(_state(issue("gpu", number=1, lane=Lane.LOCAL, labels=MARKED)))
        machine = FakeWorkstation()
        dispatcher(api, machine).recover(items(api))
        assert any("-['dispatch:local']" in call for call in api.calls)

    def test_a_retained_failure_is_cleared_before_the_retry(self) -> None:
        # The worktree path is not attempt-scoped, so the old one would make
        # `git worktree add` fail and the task could never start again.
        api = FakeGitHub(_state(issue("gpu", number=1, lane=Lane.LOCAL, labels=MARKED)))
        machine = FakeWorkstation()
        machine.existing = (
            Worktree(_ref(api), ROOT / "demo" / "gpu", "dispatchkit/gpu/1", commits=2),
        )
        dispatcher(api, machine).serve(items(api), now=NOW)
        assert machine.removed[0] == ROOT / "demo" / "gpu"
        assert machine.created == [ROOT / "demo" / "gpu"]

    def test_the_old_branch_is_pushed_before_it_is_cleared(self) -> None:
        api = FakeGitHub(_state(issue("gpu", number=1, lane=Lane.LOCAL, labels=MARKED)))
        machine = FakeWorkstation()
        machine.existing = (
            Worktree(_ref(api), ROOT / "demo" / "gpu", "dispatchkit/gpu/1", commits=2),
        )
        dispatcher(api, machine).serve(items(api), now=NOW)
        assert "dispatchkit/gpu/1" in machine.pushed

    def test_a_sweep_before_a_run_does_not_release_the_task_it_is_about_to_run(self) -> None:
        # The bug this test exists to prevent: a full recovery sweep here would
        # find the mark the scheduler set one line ago, see no worktree behind
        # it, call it abandoned, and release it -- so the lane would mark and
        # unmark for ever and never run anything.
        api = FakeGitHub(_state(issue("gpu", number=1, lane=Lane.LOCAL, labels=MARKED)))
        machine = FakeWorkstation()
        dispatcher(api, machine).serve(items(api), now=NOW)
        assert machine.created == [ROOT / "demo" / "gpu"]


class TestTheThread:
    def test_the_real_spawn_is_a_daemon(self) -> None:
        # So Ctrl-C stops `watch` immediately rather than waiting hours for an
        # agent. The run is lost, the mark stays on, and the next startup
        # sweep releases it -- which is what recovery is for.
        from dispatchkit.dispatcher import _thread

        started = threading.Event()
        thread = _thread(started.set)
        assert isinstance(thread, threading.Thread)
        assert thread.daemon
        started.wait(timeout=5)


def _ref(api: FakeGitHub) -> TaskRef:
    resolved, _ = build_items(api.fetch_state())
    return resolved[0].ref


def _state(*issues: IssueState) -> RepoState:
    return state_of(*issues)


class TestServingChangesAdmission:
    def test_the_local_lane_is_served_only_when_a_dispatcher_exists(self) -> None:
        # D6.0 refuses a lane nothing runs. Turning the executor on is exactly
        # what makes that refusal wrong, so the two are one switch.
        from dispatchkit.dispatcher import served_lanes

        assert Lane.LOCAL not in served_lanes(None)
        assert Lane.LOCAL in served_lanes(object())
        assert Lane.CLOUD in served_lanes(None)


class TestDoctorAsksWhetherTheRunnerExists:
    """D6.0 refuses a lane with no executor. This is the same question one
    step earlier: the lane is served, but the binary is not installed."""

    def test_a_missing_runner_fails_with_the_name_it_looked_for(self) -> None:
        from dispatchkit.doctor import check_runner

        result = check_runner("copilot", found=None)
        assert not result.ok
        assert "copilot" in result.detail

    def test_a_runner_on_the_path_passes_and_says_where(self) -> None:
        from dispatchkit.doctor import check_runner

        result = check_runner("copilot", found="/usr/local/bin/copilot")
        assert result.ok
        assert "/usr/local/bin/copilot" in result.detail

    def test_the_check_is_about_argv_zero_and_nothing_else(self) -> None:
        # The rest of the template is substitution, which `RunnerConfig`
        # already validates at load. The only thing left that can be wrong
        # here is whether the program exists.
        from dispatchkit.doctor import check_runner

        assert check_runner("", found=None).ok is False


class TestTheCommandLine:
    def test_local_without_push_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A dry run cannot start a real agent, and pretending it might would
        # be the one lie the whole `--push` convention exists to prevent.
        from dispatchkit.cli import main

        code = main(["watch", "--once", "--state", "does-not-matter.json", "--local"])
        assert code != 0
        assert "--push" in capsys.readouterr().err

    def test_doctor_names_the_runner_it_could_not_find(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from dispatchkit.cli import main

        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "dispatchkit.toml").write_text(
            '[runner]\nargv = ["definitely-not-installed-xyz", "{prompt}"]\n'
        )
        (tmp_path / "plans").mkdir()
        main(["doctor", "--root", str(tmp_path), "--local"])
        assert "definitely-not-installed-xyz" in capsys.readouterr().out
