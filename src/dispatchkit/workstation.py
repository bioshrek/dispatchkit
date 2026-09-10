"""D6.2: the workstation — what the local lane needs from this machine.

The port, and the pure decisions that surround it. Everything here is
arithmetic on strings and paths; `workstation_cli.py` is the half that shells
out, and `local.py` is the half that sequences them.

Three of these are load-bearing rather than incidental.

**The branch carries the attempt number.** A second run of the same task would
otherwise force-push over the evidence the first one left, and evidence is the
only thing a failed run produces.

**The prompt is the issue's prose and nothing else.** `depends`, `touches`,
`verify` and `spend` are scheduling inputs the agent cannot act on, and one of
them actively misleads: `touches` is an advisory exclusion hint, not a
permission boundary, so handing it over invites an agent to treat a scheduling
guess as a constraint on its work.

**`acceptance` is split, never shelled.** It is a human-written string that may
hold `&&`, so it becomes a list of argv lists — the same split the
`acceptance-not-in-ci` lint already uses — and each clause runs as a child
process with no shell anywhere in the path.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from dispatchkit.block import CLOSE, OPEN
from dispatchkit.model import TaskId, TaskRef

#: Where worktrees live. One directory per task, so a run's evidence is
#: findable by name, and the whole tree is walkable at startup — recovery
#: reads the local disk rather than GitHub's claims.
WORK = Path(".dispatchkit") / "work"


def work_root(home: Path) -> Path:
    return home / WORK


def worktree_path(root: Path, ref: TaskRef) -> Path:
    """`<root>/<plan>/<id>`. Qualified by plan because `TaskId` is only unique
    within one, and two plans may both contain `ports` (D13)."""
    return root / ref.plan / str(ref.id)


def ref_of(root: Path, path: Path) -> TaskRef | None:
    """Read a worktree's path back as the task it belongs to.

    Recovery walks the disk, so the path is the only identifier it has. A
    directory that does not fit the layout belongs to somebody else and is
    left alone.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) != 2:
        return None
    plan, task = relative.parts
    return TaskRef(plan, TaskId(task))


def branch_name(ref: TaskRef, attempt: int) -> str:
    """`dispatchkit/<plan>/<id>/<attempt>`.

    The attempt is in the name so a retry cannot force-push over the branch the
    previous run left behind. That branch is evidence a human may want to read,
    and nothing in the system reads it back.
    """
    return f"dispatchkit/{ref.plan}/{ref.id}/{attempt}"


def prompt_for(title: str, body: str) -> str:
    """What the agent is asked to do: the issue, minus the scheduler's half."""
    return f"{title}\n\n{strip_block(body)}".strip()


def strip_block(body: str) -> str:
    """The prose, with the machine block removed.

    Removed rather than merely ignored: the block is the scheduler's private
    vocabulary, and `touches` in particular would read to an agent as a list of
    files it is allowed to edit, which is not what it means.
    """
    start = body.find(OPEN)
    if start == -1:
        return body.strip()
    end = body.find(CLOSE, start)
    if end == -1:
        return body[:start].strip()
    return (body[:start] + body[end + len(CLOSE) :]).strip()


#: How `apply` writes the definition of done into an issue body.
ACCEPTANCE_HEADING = "## Acceptance"
FENCE = "```"


def acceptance_of(body: str) -> str:
    """Recover the acceptance command from the issue body.

    It lives in the prose rather than the machine block on purpose: both lanes
    and the reviewer run the same check, and the definition of done belongs on
    the issue rather than inside a runner. That makes this a parse of
    attacker-influencable text, so it is the narrowest one that works — find
    the heading, take the first fenced block after it, and accept nothing else.
    Anything unexpected yields no command, and a task with no command does not
    run, because nothing could then decide whether it was done.
    """
    start = body.find(ACCEPTANCE_HEADING)
    if start == -1:
        return ""
    opening = body.find(FENCE, start)
    if opening == -1:
        return ""
    opening = body.find("\n", opening)
    closing = body.find(FENCE, opening)
    if opening == -1 or closing == -1:
        return ""
    return body[opening:closing].strip()


def acceptance_argv(command: str) -> tuple[tuple[str, ...], ...]:
    """Split `acceptance` into argv lists, one per `&&` clause.

    The string comes from a graph file and is echoed into an issue body, so it
    is attacker-influencable twice over. `shlex.split` gives us the words a
    shell would have found without letting a shell find them: no expansion, no
    substitution, no chaining beyond the `&&` we split on ourselves.
    """
    clauses = []
    for clause in command.split("&&"):
        words = shlex.split(clause.strip())
        if words:
            clauses.append(tuple(words))
    return tuple(clauses)


@dataclass(frozen=True, slots=True)
class Worktree:
    """A directory on this disk, and what it is worth."""

    ref: TaskRef
    path: Path
    branch: str
    #: Commits ahead of the base. The only question recovery asks of a
    #: worktree, because it is the only thing in one that cannot be recreated.
    commits: int = 0


@dataclass(frozen=True, slots=True)
class RunResult:
    argv: tuple[str, ...]
    exit_code: int
    output: str = ""
    #: Killed by its own supervisor rather than exiting. The one case recovery
    #: cannot see, so it is handled where it happens.
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Workstation(Protocol):
    """The port `local.py` needs. The adapter lives in `workstation_cli.py`.

    Deliberately narrow: git plumbing plus one supervised child. Pushing is
    here rather than on the child's side of the fence because the child runs
    with no credential and no agent socket, which is what keeps an
    agent-authored `acceptance` away from the one that can write to the repo.
    """

    def worktrees(self) -> tuple[Worktree, ...]: ...

    def create_worktree(self, *, path: Path, branch: str) -> None: ...

    def remove_worktree(self, *, path: Path) -> None: ...

    def commits(self, *, path: Path) -> int: ...

    def push(self, *, path: Path, branch: str) -> RunResult: ...

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: timedelta,
    ) -> RunResult: ...
