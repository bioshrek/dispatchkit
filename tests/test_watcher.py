"""D13.1e — the graph watcher: a save cuts the wait short.

The loop already sleeps between passes. This is the borrowed half of the `vite`
comparison: the graph file is local, so a save can re-validate, re-lint and
re-plan in milliseconds, and there is no reason to make a developer who has
just fixed a dependency wait out the rest of an interval to see it.

What does *not* transfer is the harmlessness of the output, so nothing here
mutates anything. A save cuts the wait short and re-plans; `apply` remains the
explicit, separate act. The tests below therefore assert on *when the wait
returns and what it says*, and never on an operation.

There is no `watchdog` and never will be: zero runtime dependencies means the
mtimes are polled from the loop that was already sleeping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.cli import main
from dispatchkit.github import RepoState
from dispatchkit.watcher import GraphWatcher, Save, compare

from .fake_github import FakeGitHub
from .items import issue, state_of

pytestmark = pytest.mark.unit

GRAPH = """plan = "demo"

[[task]]
id = "a"
title = "Do the thing"
milestone = "M1"
lane = "cloud"
acceptance = "uv run pytest -q"
touches = ["src/*"]
"""


class Clock:
    """A sleep that never sleeps, so a settle window costs no wall time.

    Every test in this file would otherwise pay the debounce in real seconds,
    and a test suite that is slow because it is *correct* is the kind that
    stops being run.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Disk:
    """A plans directory that changes when a test says it does."""

    def __init__(self, **plans: tuple[int, int]) -> None:
        self.plans = dict(plans)
        self.looks = 0

    def look(self) -> dict[str, tuple[int, int]]:
        self.looks += 1
        return dict(self.plans)


def watcher(disk: Disk, clock: Clock, **kwargs: object) -> GraphWatcher:
    return GraphWatcher(
        Path("docs/plans"),
        look=disk.look,
        sleep=clock.sleep,
        **kwargs,  # type: ignore[arg-type]
    )


class TestComparingTwoLooks:
    def test_nothing_changed_is_falsey(self) -> None:
        # `if saved:` is how the loop asks, so the empty answer must be false.
        assert not compare({"a": (1, 2)}, {"a": (1, 2)})

    def test_a_new_file_is_added(self) -> None:
        assert compare({}, {"a": (1, 2)}) == Save(added=("a",))

    def test_a_deleted_file_is_removed(self) -> None:
        assert compare({"a": (1, 2)}, {}) == Save(removed=("a",))

    def test_a_rewritten_file_is_edited(self) -> None:
        assert compare({"a": (1, 2)}, {"a": (9, 2)}) == Save(edited=("a",))

    def test_a_same_length_edit_is_still_noticed(self) -> None:
        # Fixing a typo in an id changes no byte count. mtime is what carries
        # this, which is why size alone would not do.
        assert compare({"a": (1, 40)}, {"a": (2, 40)}) == Save(edited=("a",))

    def test_a_touched_file_with_the_same_content_still_counts(self) -> None:
        # Hashing the file would be more precise and would mean reading every
        # graph on every poll. A spurious re-plan costs a few milliseconds of
        # pure functions and prints one line, so precision is not worth buying.
        assert compare({"a": (1, 40)}, {"a": (2, 40)})

    def test_several_plans_are_reported_together(self) -> None:
        before = {"a": (1, 1), "b": (1, 1), "c": (1, 1)}
        after = {"a": (2, 1), "c": (1, 1), "d": (1, 1)}
        assert compare(before, after) == Save(added=("d",), edited=("a",), removed=("b",))

    def test_names_are_sorted(self) -> None:
        # A directory listing has no order worth trusting, and a report that
        # reshuffles between passes is one a reader stops comparing.
        assert compare({}, {"z": (1, 1), "a": (1, 1)}).added == ("a", "z")

    def test_it_names_the_plans_it_saw(self) -> None:
        assert Save(added=("b",), edited=("a",), removed=("c",)).describe() == (
            "a edited, b added, c removed"
        )


class TestCuttingTheWaitShort:
    def test_an_untouched_interval_is_waited_out(self) -> None:
        clock, disk = Clock(), Disk(a=(1, 1))
        assert not watcher(disk, clock).wait(30)
        assert clock.now == pytest.approx(30)

    def test_a_save_returns_early(self) -> None:
        clock, disk = Clock(), Disk(a=(1, 1))
        watch = watcher(disk, clock, poll=0.5, settle=1.0)
        disk.plans["a"] = (2, 1)
        assert watch.wait(30) == Save(edited=("a",))
        assert clock.now < 30

    def test_the_report_survives_the_settle_window(self) -> None:
        # The settle window polls again, and the second look must be compared
        # with the baseline rather than with the first look — otherwise a file
        # that stopped changing looks unchanged and the save is swallowed.
        clock, disk = Clock(), Disk(a=(1, 1))
        watch = watcher(disk, clock, poll=0.5, settle=1.0)
        disk.plans["a"] = (2, 1)
        assert watch.wait(30) == Save(edited=("a",))

    def test_a_write_in_progress_is_waited_out(self) -> None:
        # An editor writes in stages, and a graph read mid-write parses as a
        # syntax error the developer did not make. Nothing is reported until
        # the directory has held still for the settle window.
        clock, disk = Clock(), Disk(a=(1, 10))
        writes = iter([(2, 0), (3, 5), (4, 10)])

        def look() -> dict[str, tuple[int, int]]:
            disk.plans["a"] = next(writes, disk.plans["a"])
            return dict(disk.plans)

        watch = GraphWatcher(
            Path("docs/plans"), look=look, sleep=clock.sleep, poll=0.5, settle=1.0
        )
        assert watch.wait(30) == Save(edited=("a",))
        assert clock.now >= 1.0

    def test_the_interval_is_still_the_ceiling(self) -> None:
        # A file saved every poll must not hold the loop open forever: the
        # scheduler has other reasons to run, and a pull request that went
        # green is one of them.
        clock = Clock()
        step = iter(range(1, 500))

        def look() -> dict[str, tuple[int, int]]:
            return {"a": (next(step), 1)}

        watch = GraphWatcher(
            Path("docs/plans"), look=look, sleep=clock.sleep, poll=0.5, settle=1.0
        )
        assert watch.wait(5)
        assert clock.now <= 6

    def test_a_save_during_a_pass_is_not_missed(self) -> None:
        # The baseline is whatever was last *reported*, not whatever the disk
        # held when the wait began. A save landing while a pass is talking to
        # GitHub would otherwise be swallowed silently.
        clock, disk = Clock(), Disk(a=(1, 1))
        watch = watcher(disk, clock, poll=0.5, settle=1.0)
        disk.plans["a"] = (2, 1)  # saved "while the pass ran"
        assert watch.wait(30) == Save(edited=("a",))

    def test_a_reported_save_is_not_reported_twice(self) -> None:
        clock, disk = Clock(), Disk(a=(1, 1))
        watch = watcher(disk, clock, poll=0.5, settle=1.0)
        disk.plans["a"] = (2, 1)
        assert watch.wait(30)
        assert not watch.wait(30)

    def test_a_zero_interval_still_looks_once(self) -> None:
        # `--interval` has a floor of 1s at the CLI, but the ceiling arithmetic
        # must not depend on that: a wait that never looks would never notice.
        clock, disk = Clock(), Disk()
        watch = watcher(disk, clock, poll=0.5, settle=1.0)
        disk.plans["a"] = (1, 1)
        assert watch.wait(0) == Save(added=("a",))


class TestReadingTheDirectory:
    def test_it_finds_the_graph_files(self, tmp_path: Path) -> None:
        (tmp_path / "demo.tasks.toml").write_text("plan = 'demo'\n")
        (tmp_path / "other.tasks.toml").write_text("plan = 'other'\n")
        assert sorted(GraphWatcher(tmp_path).look()) == ["demo", "other"]

    def test_it_ignores_everything_that_is_not_a_graph(self, tmp_path: Path) -> None:
        # `docs/plans/` holds a README in this very repository, and editing it
        # is not a reason to re-plan.
        (tmp_path / "README.md").write_text("notes\n")
        (tmp_path / "scratch.toml").write_text("x = 1\n")
        (tmp_path / "demo.tasks.toml").write_text("plan = 'demo'\n")
        assert sorted(GraphWatcher(tmp_path).look()) == ["demo"]

    def test_a_missing_directory_is_empty_rather_than_an_error(self, tmp_path: Path) -> None:
        # `watch` may legitimately run outside a checkout — the graph lives on
        # GitHub too. A watcher that crashed there would take the scheduler
        # down with it for the sake of a convenience.
        assert GraphWatcher(tmp_path / "nowhere").look() == {}

    def test_a_directory_appearing_later_is_picked_up(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        watch = GraphWatcher(plans)
        assert watch.look() == {}
        plans.mkdir()
        (plans / "demo.tasks.toml").write_text("plan = 'demo'\n")
        assert sorted(watch.look()) == ["demo"]


class TestTheLoopUsesIt:
    """The wiring: a save cuts the wait short and says what it changed.

    Nothing here mutates. `apply` stays the explicit act, because every
    operation writes to a permanent, public, notifying artifact and an issue
    body updated on each keystroke emails everyone watching it.
    """

    def _repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        plans = tmp_path / "plans"
        plans.mkdir()
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dispatchkit.toml").write_text(f'[paths]\nplans = "{plans.name}"\n')
        return plans

    def _api(self, monkeypatch: pytest.MonkeyPatch, *, passes: int) -> FakeGitHub:
        api = FakeGitHub(state=state_of(issue("a", 1)))
        seen = 0
        real = api.fetch_state

        def fetch() -> RepoState:
            nonlocal seen
            seen += 1
            if seen > passes:
                raise KeyboardInterrupt
            return real()

        monkeypatch.setattr(api, "fetch_state", fetch)
        monkeypatch.setattr("dispatchkit.cli.GhCli", lambda **_: api)
        return api

    def _run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        main(["watch", "--push", "--repo", "o/n", "--config", str(tmp_path / "dispatchkit.toml")])

    def test_a_save_is_reported_and_re_planned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plans = self._repo(tmp_path, monkeypatch)
        self._api(monkeypatch, passes=2)

        def sleep(seconds: float) -> None:
            (plans / "demo.tasks.toml").write_text(GRAPH)

        monkeypatch.setattr("dispatchkit.cli.time.sleep", sleep)
        self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "saved demo added" in out
        assert "1 task" in out

    def test_a_saved_graph_that_does_not_parse_is_reported_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A file caught mid-thought is the normal state of one being edited.
        # The loop says so and keeps running; it does not exit on a typo.
        plans = self._repo(tmp_path, monkeypatch)
        self._api(monkeypatch, passes=3)

        def sleep(seconds: float) -> None:
            (plans / "demo.tasks.toml").write_text("plan = 'demo'\n[[task]]\nid = 'a'\n")

        monkeypatch.setattr("dispatchkit.cli.time.sleep", sleep)
        self._run(monkeypatch, tmp_path)

        captured = capsys.readouterr()
        assert "does not validate" in captured.out
        assert "stopped" in captured.out

    def test_a_deleted_graph_says_what_it_does_not_do(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Deleting a file cannot close an issue. `apply` already refuses to,
        # reporting `orphan-issue`, and silence here would let a developer
        # believe a deletion had taken effect on GitHub.
        plans = self._repo(tmp_path, monkeypatch)
        (plans / "demo.tasks.toml").write_text(GRAPH)
        self._api(monkeypatch, passes=2)

        def sleep(seconds: float) -> None:
            (plans / "demo.tasks.toml").unlink(missing_ok=True)

        monkeypatch.setattr("dispatchkit.cli.time.sleep", sleep)
        self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "saved demo removed" in out
        assert "not closed" in out

    def test_an_unedited_interval_says_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._repo(tmp_path, monkeypatch)
        self._api(monkeypatch, passes=2)
        monkeypatch.setattr("dispatchkit.cli.time.sleep", lambda seconds: None)
        self._run(monkeypatch, tmp_path)
        assert "saved" not in capsys.readouterr().out

    def test_a_single_pass_never_watches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--once` has no wait to cut short, and `watch --once | tee` has to
        # stay plain.
        plans = self._repo(tmp_path, monkeypatch)
        (plans / "demo.tasks.toml").write_text(GRAPH)
        self._api(monkeypatch, passes=5)
        main(
            [
                "watch",
                "--once",
                "--push",
                "--repo",
                "o/n",
                "--config",
                str(tmp_path / "dispatchkit.toml"),
            ]
        )
        assert "saved" not in capsys.readouterr().out
