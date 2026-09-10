"""D6.5: serving the local lane, without holding up a pass.

Two components in one process. The scheduler emits `dispatch:local` and moves
on; the dispatcher discovers work by reading issue state, exactly as a cloud
agent does. They share no queue and no memory, so a two-hour local task cannot
stop a pass from merging a cloud one, and the dispatcher holds nothing the next
pass could not recompute.

The thread is a worker, not a place decisions live. It is asked one question —
"is it still going?" — and every other fact is re-derived from GitHub each
pass. That is what makes it safe rather than merely convenient: a restart loses
nothing except the run itself, and recovery is what turns a lost run back into
a queued task.

**Recovery is scoped, not swept, before a run.** A full sweep here would find
the mark the scheduler set moments ago, see no worktree behind it, call it
abandoned and release it — so the lane would mark and unmark for ever and never
run anything. The whole-machine sweep belongs to startup, when nothing is ours
yet; before a run, only that task's own leftovers are in question.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import LABEL_LOCAL_CLAIM, GitHubApi
from dispatchkit.local import LocalRun, run_local
from dispatchkit.model import Lane, TaskRef
from dispatchkit.recover import execute_recovery, plan_recovery
from dispatchkit.resolve import TaskItem
from dispatchkit.tick import SERVED_LANES
from dispatchkit.workstation import Workstation


class Running(Protocol):
    """The two things the dispatcher asks of whatever it started."""

    def is_alive(self) -> bool: ...

    def wait(self) -> None:
        """Block until it is finished. Only `--once` calls this (D6.6)."""


class _Thread:
    """A daemon thread, plus the one way to wait for it.

    Daemon so that Ctrl-C stops `watch` now rather than in two hours. The run
    is lost when that happens: the mark stays on, and the next startup sweep
    releases it, which is precisely what recovery is for and why an interrupt
    does not need to be graceful.

    But finishing normally is not an interrupt, and `--once` was relying on the
    daemon flag to end a run it had just started and reported as dispatched
    (D6.6). `wait` is what that mode needed and did not have.
    """

    def __init__(self, target: Callable[[], None]) -> None:
        self.thread = threading.Thread(target=target, name="dispatchkit-local", daemon=True)
        self.thread.start()

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def wait(self) -> None:
        self.thread.join()


def _thread(target: Callable[[], None]) -> Running:
    return _Thread(target)


def served_lanes(dispatcher: object | None) -> frozenset[Lane]:
    """D6.0 refuses a lane nothing runs. Turning the executor on is exactly
    what makes that refusal wrong, so the two are one switch rather than two
    settings that can disagree."""
    if dispatcher is None:
        return frozenset(SERVED_LANES)
    return frozenset(SERVED_LANES) | {Lane.LOCAL}


@dataclass
class LocalDispatcher:
    """One machine, one task at a time."""

    api: GitHubApi
    machine: Workstation
    config: SchedulerConfig
    root: Path
    spawn: Callable[[Callable[[], None]], Running] = _thread
    #: The last run, for the pass that wants to report it. Rendering only:
    #: nothing here is ever read back into a decision.
    last: LocalRun | None = None
    #: What is on this machine right now, for the pass that wants to say so.
    running: TaskRef | None = None
    _running: Running | None = field(default=None, repr=False)

    @property
    def busy(self) -> bool:
        return self._running is not None and self._running.is_alive()

    def wait(self) -> None:
        """Block until the run in flight is done. A no-op when there is none.

        Called only where there is no next pass for a running task to hold up,
        which is exactly `--once`. In the loop this is never called, because
        not waiting is the whole of D6.5.
        """
        if self._running is not None:
            self._running.wait()

    def take_finished(self) -> LocalRun | None:
        """The last run, once. Reported by the next pass and then forgotten,
        so a run is announced exactly as many times as it happened."""
        finished, self.last = self.last, None
        return finished

    def recover(self, items: Sequence[TaskItem]) -> tuple[str, ...]:
        """The startup sweep. Every mark is abandoned, because this is the
        only dispatcher and it has only just started."""
        return execute_recovery(
            plan_recovery(self.machine.worktrees(), items, root=self.root),
            api=self.api,
            machine=self.machine,
        )

    def serve(
        self,
        items: Sequence[TaskItem],
        *,
        now: datetime,
        marked: Collection[TaskRef] = (),
    ) -> TaskItem | None:
        """Start at most one task, and return without waiting for it.

        `marked` is what the pass just labelled. The items were read *before*
        that write, so without it a `--once` pass would mark a task and then
        find nothing to run.
        """
        if self.busy:
            return None
        task = _claimed(items, marked)
        if task is None:
            return None
        self.running = task.ref
        self._running = self.spawn(lambda: self._run(task, now, items))
        return task

    def _clear(self, task: TaskItem, items: Sequence[TaskItem]) -> None:
        """Recover only this task's leftovers — see the module docstring for
        why the sweep is not run here."""
        mine = [tree for tree in self.machine.worktrees() if tree.ref == task.ref]
        if not mine:
            return
        # `release=False` throughout: the mark is this run's claim, and taking
        # it off is what the run itself does when it ends.
        plan = tuple(
            replace(entry, release=False)
            for entry in plan_recovery(mine, items, root=self.root)
        )
        execute_recovery(plan, api=self.api, machine=self.machine)

    def _run(self, task: TaskItem, now: datetime, items: Sequence[TaskItem]) -> None:
        """The thread's whole body, so nothing escapes it.

        A thread that raises prints to stderr and vanishes, leaving `last`
        unset — the pass then says "did not report" about a failure it had the
        reason for, and exits 0. `run_local` handles its own stages; this is
        the last resort for everything around them, including `_clear` and the
        machine calls it makes before the run proper starts (D6.6).
        """
        try:
            # Inside the guard, and on the thread: clearing is part of the run,
            # and a machine that raises here used to take the pass down with it
            # rather than failing the one task (D6.6).
            self._clear(task, items)
            self.last = run_local(
                task,
                api=self.api,
                machine=self.machine,
                config=self.config,
                root=self.root,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - the alternative is a silent thread
            detail = f"{type(exc).__name__}: {exc}"
            self.last = LocalRun(str(task.ref), "", "dispatcher", False, detail)
            _release(self.api, task, detail)


def _release(api: GitHubApi, task: TaskItem, detail: str) -> None:
    """Say what happened and unclaim the task, best effort.

    Best effort because this runs when something already went wrong, and the
    thing that went wrong may be the API itself. A mark left on is recoverable
    — `recover` releases a claim with no worktree — but a thread that dies
    while trying to report is not.
    """
    try:
        api.comment(number=task.number, body=f"`dispatchkit` stopped: {detail}")
        api.edit_labels(number=task.number, remove=(LABEL_LOCAL_CLAIM,))
    except Exception:  # noqa: BLE001,S110 - nothing left to report it to
        pass


def _claimed(items: Sequence[TaskItem], marked: Collection[TaskRef] = ()) -> TaskItem | None:
    """The one marked, open, local task, in issue order.

    The lane is checked as well as the mark. A label is something a human can
    type, and "somebody typed it" is not authority to run arbitrary work on
    this machine.
    """
    for task in sorted(items, key=lambda item: item.number):
        if task.closed or task.lane is not Lane.LOCAL:
            continue
        if LABEL_LOCAL_CLAIM in task.labels or task.ref in marked:
            return task
    return None
