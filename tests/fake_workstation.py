"""The in-memory workstation, for testing the local lane offline.

The same standard as `fake_github.py`: it records what it was asked to do and
answers from a dict, so a whole local run — worktree, agent, acceptance, push,
pull request — is exercised without a subprocess, a git repository or a
network. `tests/conftest.py` makes the network refusal literal; this makes the
*machine* refusal literal, which matters more here, because the thing under
test is a program that starts other programs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from dispatchkit.workstation import RunResult, Worktree

ROOT = Path("/work")


@dataclass
class FakeWorkstation:
    """Scripted outcomes in, a transcript out."""

    #: argv[0] -> the result that command should produce. Anything unscripted
    #: succeeds silently, so a test says only what it cares about.
    outcomes: dict[str, RunResult] = field(default_factory=dict)
    #: Worktrees already on disk when the test starts, for recovery.
    existing: tuple[Worktree, ...] = ()
    #: Commits found in a worktree, by path.
    commit_counts: dict[Path, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    runs: list[tuple[tuple[str, ...], Path, Mapping[str, str]]] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    push_fails: bool = False

    def worktrees(self) -> tuple[Worktree, ...]:
        self.calls.append("worktrees()")
        return self.existing

    def create_worktree(self, *, path: Path, branch: str) -> None:
        self.calls.append(f"create_worktree({path}, {branch})")
        self.created.append(path)

    def remove_worktree(self, *, path: Path) -> None:
        self.calls.append(f"remove_worktree({path})")
        self.removed.append(path)

    def commits(self, *, path: Path) -> int:
        self.calls.append(f"commits({path})")
        return self.commit_counts.get(path, 0)

    def push(self, *, path: Path, branch: str) -> RunResult:
        self.calls.append(f"push({branch})")
        if self.push_fails:
            return RunResult(("git", "push"), 1, "rejected")
        self.pushed.append(branch)
        return RunResult(("git", "push"), 0)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: timedelta,
    ) -> RunResult:
        self.calls.append(f"run({argv[0]})")
        self.runs.append((tuple(argv), cwd, dict(env)))
        return self.outcomes.get(argv[0], RunResult(tuple(argv), 0, "ok"))
