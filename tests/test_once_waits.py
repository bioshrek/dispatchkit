"""D6.6: `--once --local` finishes the task it started.

Found live. `watch --once --push --local` marked issue #13 with
`dispatch:local`, started the agent on its daemon thread, printed
`local: started wordfreq/version-flag`, reported `pass complete: 1 dispatched`
and exited 0 — all within a second. The thread died with the process. The
issue was left claimed, with no worktree, no branch, no pull request and no
comment saying anything had gone wrong.

So the mode does not merely fail to finish the work: it reports success, and
it *burns an attempt* doing it, because attempts are derived from the mark
events on the timeline. Three runs of a documented command turn a task nobody
has ever actually executed into `dispatch:stuck`.

The thread was always right — it exists so that a running local task does not
hold up the *next pass*, which is D6.5's property and is preserved below. But
`--once` has no next pass. There is nothing for the thread to avoid holding
up, so the only thing it can do is drop the work on the floor.

Ctrl-C is different, and stays different: an interrupt abandons the run and
lets the startup sweep release the mark, which is what recovery is for.
Finishing normally is not an interrupt.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.dispatcher import LocalDispatcher
from dispatchkit.github import LABEL_LOCAL_CLAIM
from dispatchkit.model import Lane
from dispatchkit.resolve import TaskItem, build_items
from tests.fake_github import FakeGitHub
from tests.fake_workstation import ROOT, FakeWorkstation
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
CONFIG = SchedulerConfig()
MARKED = ("dispatchkit", LABEL_LOCAL_CLAIM)


def api_with_work() -> FakeGitHub:
    return FakeGitHub(state=state_of(issue("gpu", number=1, lane=Lane.LOCAL, labels=MARKED)))


def items(api: FakeGitHub) -> Sequence[TaskItem]:
    resolved, _ = build_items(api.fetch_state())
    return resolved


class Joinable:
    """A 'thread' that finishes only when it is waited for."""

    def __init__(self, target: Callable[[], None]) -> None:
        self.target = target
        self.done = False

    def is_alive(self) -> bool:
        return not self.done

    def wait(self) -> None:
        self.target()
        self.done = True


class TestTheDispatcherCanBeWaitedFor:
    def _dispatcher(self, api: FakeGitHub, machine: FakeWorkstation) -> LocalDispatcher:
        return LocalDispatcher(
            api=api, machine=machine, config=CONFIG, root=ROOT, spawn=Joinable
        )

    def test_it_is_busy_until_waited_for(self) -> None:
        api = api_with_work()
        local = self._dispatcher(api, FakeWorkstation())
        started = local.serve(items(api), now=NOW, marked=())

        assert started is not None
        assert local.busy

    def test_waiting_finishes_the_run(self) -> None:
        api = api_with_work()
        local = self._dispatcher(api, FakeWorkstation())
        local.serve(items(api), now=NOW, marked=())

        local.wait()

        assert not local.busy
        # The run happened and is there to be reported.
        assert local.last is not None

    def test_waiting_for_nothing_is_fine(self) -> None:
        # Every `--once` pass calls it; almost none of them started anything.
        local = self._dispatcher(api_with_work(), FakeWorkstation())
        local.wait()
