"""D6.6: a failed pass is reported, not raised.

The local lane's first live run died on an unknown label, and what reached the
terminal was a twelve-frame traceback out of `execute_tick`. The defect it was
reporting was real, but the *shape* of the report was the D11 bug again: the
adapter's `RuntimeError` is a contract with its callers, and `watch` was not
holding up its end.

For `--once` that is merely ugly. For the loop it is a liveness bug. Everything
`gh` can do to you overnight — a rate limit, a 502, a token expiring, a network
blip — arrives as exactly this exception, and the scheduler is a process meant
to be left running. Dying on the first one means the pass that would have
succeeded ten minutes later never happens.

Continuing is safe here for a reason specific to this design, not because
retrying is generally nice: there is no stored state, so a half-finished pass
leaves only the operations it already sent, and the next pass re-derives
everything from the issues. That is the same property the `KeyboardInterrupt`
handler already relies on.

A permanent failure therefore repeats every interval rather than being
swallowed. That is deliberate: loud and repeating is legible, and an operator
can stop it. Silently exiting at 3am is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.cli import EXIT_OK, main
from dispatchkit.github import RepoState
from tests.fake_github import FakeGitHub
from tests.items import issue, state_of

pytestmark = pytest.mark.unit


class Failing(FakeGitHub):
    """Fails the first `n` dispatches, at the point the live run failed.

    `fetch_state` was already guarded, which is why the hole survived: the
    failure has to come from *executing* the plan rather than from reading the
    state, and executing it is the half that only `--push` reaches.
    """

    def __init__(self, failures: int) -> None:
        super().__init__(state=state_of(issue("a", 1)))
        self.remaining = failures
        self.passes = 0

    def fetch_state(self) -> RepoState:
        self.passes += 1
        return super().fetch_state()

    def assign_agent(self, *, number: int, node_id: str) -> None:
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("gh issue failed: 'dispatch:local' not found")
        super().assign_agent(number=number, node_id=node_id)


def _stop(monkeypatch: pytest.MonkeyPatch, api: Failing, passes: int) -> None:
    read = api.fetch_state

    def fetch_state() -> RepoState:
        if api.passes >= passes:
            raise KeyboardInterrupt
        return read()

    monkeypatch.setattr(api, "fetch_state", fetch_state)


def _quiet_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dispatchkit.cli.time.sleep", lambda _: None)


class TestASinglePass:
    def test_it_does_not_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("dispatchkit.cli.GhCli", lambda **_: Failing(failures=1))

        # The bug: this raised RuntimeError instead of returning.
        code = main(
            ["watch", "--once", "--push", "--repo", "o/n", "--config", str(tmp_path / "no.toml")]
        )

        assert code != EXIT_OK

    def test_it_says_what_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("dispatchkit.cli.GhCli", lambda **_: Failing(failures=1))

        main(["watch", "--once", "--push", "--repo", "o/n", "--config", str(tmp_path / "no.toml")])

        assert "dispatch:local" in capsys.readouterr().err


class TestTheLoopSurvivesIt:
    def test_a_later_pass_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        api = Failing(failures=1)
        monkeypatch.setattr("dispatchkit.cli.GhCli", lambda **_: api)
        _quiet_clock(monkeypatch)
        _stop(monkeypatch, api, passes=3)

        main(["watch", "--push", "--repo", "o/n", "--config", str(tmp_path / "no.toml")])

        # One pass failed and two more happened: the loop did not die on it.
        assert api.passes >= 3

    def test_the_failure_is_still_reported_every_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A permanent failure is loud and repeating rather than swallowed.
        api = Failing(failures=99)
        monkeypatch.setattr("dispatchkit.cli.GhCli", lambda **_: api)
        _quiet_clock(monkeypatch)
        _stop(monkeypatch, api, passes=3)

        main(["watch", "--push", "--repo", "o/n", "--config", str(tmp_path / "no.toml")])

        assert capsys.readouterr().err.count("dispatch:local") >= 2
