"""Notice a saved graph file, and cut the wait short (D13.1e).

The loop already sleeps between passes, and the graph file is local: a save can
be re-validated, re-linted and re-planned in milliseconds, so making a
developer who has just fixed a dependency wait out the rest of an interval to
see it is a cost with nothing on the other side of it.

What is *not* borrowed from a front-end reloader is the harmlessness of the
output. `vite` writes to `dist/`; every operation here writes to a permanent,
public, notifying artifact, and an issue body updated on each keystroke emails
everyone watching it. So this module cuts a wait short and says what moved. It
mutates nothing — `apply` stays the explicit, separate act.

Zero runtime dependencies means no `watchdog`, and none is wanted: a plans
directory holds a handful of files, the loop was sleeping anyway, and one
`os.stat` per file per poll is cheaper than the machinery for avoiding it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import GRAPH_SUFFIX

__all__ = ["GraphWatcher", "Save", "Snapshot", "compare"]

#: Plan name to the pair that stands in for its contents. Both halves are
#: needed: a size alone misses a typo fix, which changes no byte count, and an
#: mtime alone is enough only until a filesystem rounds it.
Snapshot = Mapping[str, tuple[int, int]]

POLL = 0.25
SETTLE = 0.4


@dataclass(frozen=True, slots=True)
class Save:
    """What the disk did while the loop was waiting.

    Three kinds rather than one, because they read differently to a person: a
    new plan is a new intent, an edit is a revision, and a removal is the one
    that can strand issues that are already open.
    """

    added: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.edited or self.removed)

    @property
    def plans(self) -> tuple[str, ...]:
        """Every plan named, once, in a stable order."""
        return tuple(sorted({*self.added, *self.edited, *self.removed}))

    def describe(self) -> str:
        pairs = [
            *((name, "added") for name in self.added),
            *((name, "edited") for name in self.edited),
            *((name, "removed") for name in self.removed),
        ]
        return ", ".join(f"{name} {what}" for name, what in sorted(pairs))


def compare(before: Snapshot, after: Snapshot) -> Save:
    """The difference between two looks at the plans directory.

    Pure, so what counts as a change is one assertable function rather than a
    condition buried in a polling loop.
    """
    both = set(before) & set(after)
    return Save(
        added=tuple(sorted(set(after) - set(before))),
        edited=tuple(sorted(name for name in both if before[name] != after[name])),
        removed=tuple(sorted(set(before) - set(after))),
    )


class GraphWatcher:
    """Wait out an interval, or a save — whichever comes first.

    The baseline is what was last *reported*, not what the disk held when the
    wait began, so a file saved while a pass is talking to GitHub is picked up
    by the next wait instead of being swallowed.
    """

    def __init__(
        self,
        plans: Path,
        *,
        look: Callable[[], Snapshot] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll: float = POLL,
        settle: float = SETTLE,
    ) -> None:
        self.plans = plans
        self._look = look if look is not None else self._scan
        self._sleep = sleep
        self._poll = poll
        self._settle = settle
        # Taken now rather than on the first wait: the first pass runs between
        # the two, and a graph saved during it is exactly the save a developer
        # is waiting to see the effect of.
        self._seen: Snapshot = self.look()

    def look(self) -> Snapshot:
        return self._look()

    def wait(self, seconds: float) -> Save:
        """Sleep up to `seconds`, returning early once a save has settled.

        The budget is counted down from what this was asked to sleep rather
        than read from a clock. One seam is enough to make the whole of this
        assertable offline, the drift is bounded by a single poll, and an
        interval is a rate rather than a deadline — nothing here is owed
        accuracy a clock would buy.

        That interval stays the ceiling. A file being written on every poll
        must not hold the loop open indefinitely, because the scheduler has
        other reasons to run — a pull request that went green is one of them.
        """
        remaining = seconds
        while True:
            found = self.look()
            if compare(self._seen, found):
                settled, remaining = self._hold(found, remaining)
                save = compare(self._seen, settled)
                self._seen = settled
                if save:
                    return save
                # Saved and then reverted inside the settle window: nothing
                # changed, so there is nothing to say and nothing to re-plan.
            if remaining <= 0:
                return Save()
            step = min(self._poll, remaining)
            self._sleep(step)
            remaining -= step

    def _hold(self, found: Snapshot, remaining: float) -> tuple[Snapshot, float]:
        """Poll until the directory stops moving, then return what it holds.

        An editor writes in stages, and a graph read mid-write parses as a
        syntax error the developer did not make. The settle window is the only
        grace the interval is given: a directory that never stops moving must
        not stop the scheduler.
        """
        while True:
            self._sleep(self._poll)
            remaining -= self._poll
            again = self.look()
            if again == found or remaining <= -self._settle:
                return again, remaining
            found = again

    def _scan(self) -> Snapshot:
        """One `os.stat` per graph file, and nothing else read.

        A missing directory is empty rather than an error: `watch` may
        legitimately run outside a checkout, because the graph lives on GitHub
        too, and a convenience must not take the scheduler down with it.
        """
        found: dict[str, tuple[int, int]] = {}
        try:
            entries = sorted(self.plans.iterdir())
        except OSError:
            return found
        for entry in entries:
            if not entry.name.endswith(GRAPH_SUFFIX):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            found[entry.name[: -len(GRAPH_SUFFIX)]] = (stat.st_mtime_ns, stat.st_size)
        return found
