"""D6.4: recovering the local lane's own work.

There is no stored state, so "what was this machine doing when it died" has
exactly one durable answer: the worktrees on its disk. This walks them,
preserves whatever cannot be recreated, and releases every claim.

It runs at startup **and** before every run, which is why it is one function
with two callers rather than two procedures. The second call is not belt and
braces: a retained failure sits at `<plan>/<id>`, which is not attempt-scoped,
so `git worktree add` would fail on the retry. Retention is for the human
looking now, not for ever.

Split the usual way — `plan_recovery` decides and `execute_recovery` acts — so
every case is a table row rather than a sequence somebody has to hold in their
head.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dispatchkit.github import LABEL_LOCAL_CLAIM, GitHubApi
from dispatchkit.model import TaskRef
from dispatchkit.resolve import TaskItem
from dispatchkit.workstation import Workstation, Worktree


class DispatcherBusy(RuntimeError):
    """Another `watch` already holds this machine."""


@dataclass(frozen=True, slots=True)
class Recovery:
    """One thing left behind, and what is to become of it."""

    ref: TaskRef
    number: int
    #: `None` when the claim has no worktree — a mark with nothing behind it.
    path: Path | None
    branch: str
    push: bool
    report: bool
    release: bool


def plan_recovery(
    worktrees: Sequence[Worktree],
    items: Sequence[TaskItem],
    *,
    root: Path,
) -> tuple[Recovery, ...]:
    """Decide what to preserve and what to release.

    Two sources, because the two failure modes are not symmetric. A worktree
    with no mark is work whose claim was already dropped; a mark with no
    worktree is a claim whose work never started, or whose process died before
    it could. Both have to be swept or the task is stranded.
    """
    by_ref = {item.ref: item for item in items}
    plan: list[Recovery] = []
    seen: set[TaskRef] = set()

    for tree in worktrees:
        item = by_ref.get(tree.ref)
        if item is None:
            # Another plan, another checkout, or a directory somebody made.
            # Deleting a stranger's tree on the strength of a path is not ours.
            continue
        seen.add(tree.ref)
        open_issue = not item.closed
        plan.append(
            Recovery(
                ref=tree.ref,
                number=item.number,
                path=tree.path,
                branch=tree.branch,
                # Commits are the only thing in a worktree that cannot be
                # recreated, so they are pushed even for a closed task: the
                # branch costs nothing and silently deleting work does not.
                push=tree.commits > 0 and bool(tree.branch),
                # But a closed task's conversation is finished. It closed
                # because its pull request merged, so the branch is already on
                # the remote and a comment would say nothing true.
                report=tree.commits > 0 and open_issue and bool(tree.branch),
                release=open_issue and LABEL_LOCAL_CLAIM in item.labels,
            )
        )

    for item in items:
        if item.ref in seen or item.closed or LABEL_LOCAL_CLAIM not in item.labels:
            continue
        # One dispatcher means a mark this process cannot account for is by
        # definition abandoned. No adoption, no heartbeat, no reclaim timeout.
        plan.append(
            Recovery(
                ref=item.ref,
                number=item.number,
                path=None,
                branch="",
                push=False,
                report=False,
                release=True,
            )
        )
    return tuple(plan)


def execute_recovery(
    plan: Sequence[Recovery],
    *,
    api: GitHubApi,
    machine: Workstation,
) -> tuple[str, ...]:
    """Preserve, then release, then discard — and never the other way round.

    Removing the worktree before the push destroys the thing being preserved,
    so a failed push keeps its tree: it is then the only copy left. The mark
    still comes off, because one bad push must not strand a task for ever —
    that is the entire class of bug this exists to end.
    """
    notes: list[str] = []
    for entry in plan:
        saved = True
        if entry.push and entry.path is not None:
            saved = machine.push(path=entry.path, branch=entry.branch).ok
        if entry.report and saved:
            api.comment(number=entry.number, body=_note(entry))
            notes.append(f"{entry.ref}: kept `{entry.branch}`")
        if entry.release:
            api.edit_labels(number=entry.number, remove=(LABEL_LOCAL_CLAIM,))
            notes.append(f"{entry.ref}: released")
        if entry.path is not None and saved:
            machine.remove_worktree(path=entry.path)
    return tuple(notes)


def _note(entry: Recovery) -> str:
    return (
        f"`dispatchkit` found an unfinished local run for this task and stopped it.\n\n"
        f"Its commits are on `{entry.branch}`, kept for inspection. The task is back in the "
        "queue and the next attempt starts from a clean tree — nothing on that branch is "
        "read back."
    )


@contextmanager
def hold_dispatcher(path: Path, *, pid: int | None = None) -> Iterator[Path]:
    """Refuse a second dispatcher on this machine, and say who holds it.

    Two would both mark and both run, with no error anywhere — the one failure
    the single-dispatcher rule exists to make impossible, so the rule needs
    something enforcing it rather than a sentence in a document.

    A lockfile whose process is gone is taken over rather than obeyed. A
    machine that crashed leaves one behind, and refusing for ever on the
    strength of a stale number would need a human to delete a file they were
    never told existed.
    """
    mine = os.getpid() if pid is None else pid
    holder = _holder(path)
    if holder is not None and holder != mine:
        raise DispatcherBusy(
            f"another dispatchkit dispatcher is running on this machine (pid {holder}). "
            f"Stop it, or delete {path} if you are sure it is gone."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{mine}\n")
    try:
        yield path
    finally:
        if _holder(path) == mine:
            path.unlink(missing_ok=True)


def _holder(path: Path) -> int | None:
    """The live pid in the lockfile, or `None` if there is not one."""
    try:
        recorded = path.read_text().strip()
    except OSError:
        return None
    if not recorded.isdigit():
        return None
    holder = int(recorded)
    try:
        os.kill(holder, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        # Alive, and somebody else's. Still a reason to refuse.
        return holder
    except OverflowError:
        return None
    return holder
