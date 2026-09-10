"""D6.2: the workstation adapter — the only other module that shells out.

`gh_cli.py` was the only one until now, and the reason for the second is that
this one runs a *different kind* of child. `gh` is ours: we built the argv and
we trust the binary. The agent is not: it is the thing being supervised, it
runs with a trimmed environment and no credential, and it is killed by the
clock rather than trusted to stop.

Everything that decides anything lives in `workstation.py`; everything that
happens lives here. The commands are pure functions so their shape is
assertable with no subprocess in the test.

Two choices worth naming:

**Worktrees branch from `origin/<base>`, never from the checkout.** The parent
repository is a tree a human is working in. Inheriting whatever they happen to
have checked out would make a run's starting point depend on somebody's
afternoon.

**The push is `--force-with-lease`.** A retry reuses no branch — the attempt
number is in the name — so a lease can only fail when something the executor
did not do has moved the branch, which is exactly when it should stop.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from dispatchkit.workstation import RunResult, Worktree, dirty_plans, ref_of

#: How much of a child's output is kept. The agent is chatty and the tail is
#: what a failure is diagnosed from; the whole of it would be posted nowhere
#: and held in memory for the length of the run.
LIMIT = 200_000


def fetch_command(remote: str, base: str) -> list[str]:
    return ["git", "fetch", "--quiet", remote, base]


def add_command(path: Path, branch: str, start: str) -> list[str]:
    return ["git", "worktree", "add", "-b", branch, str(path), start]


def remove_command(path: Path) -> list[str]:
    """`--force` because the agent is under no obligation to leave it clean,
    and the tree is disposable by construction: anything worth keeping was
    committed, and anything committed was pushed before this is reached."""
    return ["git", "worktree", "remove", "--force", str(path)]


def list_command() -> list[str]:
    return ["git", "worktree", "list", "--porcelain"]


def commits_command(base: str) -> list[str]:
    return ["git", "rev-list", "--count", f"{base}..HEAD"]


def push_command(remote: str, branch: str) -> list[str]:
    return [
        "git",
        "push",
        "--force-with-lease",
        remote,
        f"{branch}:refs/heads/{branch}",
    ]


def status_command(plans: Path) -> list[str]:
    """What is uncommitted under the plans directory, and nothing else.

    Narrowed at the git end rather than filtered afterwards: in a repository
    mid-refactor the untracked list runs to thousands of lines, none of which
    can answer the only question being asked. `--untracked-files=all` because
    a plan that has never been committed has never been reviewed, which is the
    strongest form of what the gate refuses.
    """
    return ["git", "status", "--porcelain", "--untracked-files=all", "--", str(plans)]


def _seconds(timeout: timedelta) -> float:
    return timeout.total_seconds()


def _parse_worktrees(output: str, root: Path) -> tuple[Worktree, ...]:
    """Read `git worktree list --porcelain` back into ours and not-ours.

    Records are blank-line separated, one `key value` per line. The first is
    always the main checkout, which is a human's, so the filter is not "skip
    the first" but "does this path read back as one of our tasks" — `ref_of`
    answers that, and anything it does not recognise is left alone.
    """
    # git reports the *canonical* path, which on macOS is not the one we
    # handed it -- `/tmp` is a symlink to `/private/tmp`. Matching only the
    # literal string found nothing, so recovery saw an empty machine, retained
    # worktrees were never cleared, and every retry failed on `git worktree
    # add` with the path already in use. Found by running it.
    roots = {root, root.resolve()}
    trees: list[Worktree] = []
    for record in output.split("\n\n"):
        fields: dict[str, str] = {}
        for line in record.splitlines():
            if not line.strip():
                continue
            key, _, value = line.partition(" ")
            fields[key] = value
        raw = fields.get("worktree")
        if not raw:
            continue
        path = Path(raw)
        ref = next((found for found in (ref_of(base, path) for base in roots) if found), None)
        if ref is None:
            continue
        branch = fields.get("branch", "")
        trees.append(
            Worktree(ref, path, branch.removeprefix("refs/heads/") if branch else "")
        )
    return tuple(trees)


@dataclass(frozen=True, slots=True)
class CliWorkstation:
    """This machine, reached through `git` and one supervised child."""

    root: Path
    #: Where the parent repository is. Every git command runs here, including
    #: the ones about a worktree, except the two that must run *inside* it.
    repo: Path = field(default_factory=Path.cwd)
    remote: str = "origin"
    base: str = "main"

    @property
    def _start(self) -> str:
        return f"{self.remote}/{self.base}"

    def dirty(self, *, plans: Path) -> frozenset[str]:
        """Which plans have uncommitted changes (D13.1).

        Fails *open*: a repository git cannot read dirties nothing. Failing
        closed would refuse every auto-merge on a machine where `watch` runs
        outside a checkout, which is a legitimate way to use it — the graph
        lives on GitHub as well as on disk.
        """
        result = self._git(status_command(plans))
        return dirty_plans(result.output, plans) if result.ok else frozenset()

    def worktrees(self) -> tuple[Worktree, ...]:
        """Each one, and what it holds.

        The count is a second command per worktree because `--porcelain` does
        not report it, and it is the only question recovery asks: commits are
        the one thing in a worktree that cannot be recreated. Leaving it at
        zero made every recovered tree look empty, so recovery would have
        discarded the work it exists to preserve.
        """
        listed = _parse_worktrees(self._git(list_command()).output, self.root)
        return tuple(
            replace(tree, commits=self.commits(path=tree.path)) for tree in listed
        )

    def create_worktree(self, *, path: Path, branch: str) -> RunResult:
        """The result is returned rather than dropped. A leftover branch from
        a killed run makes this fail, and swallowing it launched the agent
        into a directory that was not there — reporting the *next* thing to go
        wrong, which is true and useless to whoever reads the issue."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fetched = self._git(fetch_command(self.remote, self.base))
        if not fetched.ok:
            return fetched
        return self._git(add_command(path, branch, self._start))

    def remove_worktree(self, *, path: Path) -> None:
        self._git(remove_command(path))

    def commits(self, *, path: Path) -> int:
        """Ahead of the base, or zero if the question cannot be answered.

        No existence check: a missing directory arrives as an `OSError` from
        the spawn and becomes a failed result, and one way of saying "there is
        nothing here" is easier to be right about than two.
        """
        result = self._git(commits_command(self._start), cwd=path)
        counted = result.output.strip()
        return int(counted) if result.ok and counted.isdigit() else 0

    def push(self, *, path: Path, branch: str) -> RunResult:
        return self._git(push_command(self.remote, branch), cwd=path)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: timedelta,
    ) -> RunResult:
        """The agent. Trimmed environment, its own clock, output captured.

        A timeout is a result rather than an exception because the caller has
        to report it on the issue and take the mark off either way — losing
        the run to a traceback would leave the task claimed for ever.
        """
        return _spawn(argv, cwd=cwd, env=dict(env), timeout=_seconds(timeout))

    def _git(self, command: Sequence[str], *, cwd: Path | None = None) -> RunResult:
        return _spawn(command, cwd=cwd or self.repo, env=None, timeout=None)


def _spawn(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout: float | None,
) -> RunResult:
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell
            list(argv),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        return RunResult(tuple(argv), -1, _text(expired.output)[-LIMIT:], timed_out=True)
    except OSError as missing:
        # The runner is not installed, or the worktree is gone. Both are the
        # caller's to report, and neither is an exception it can act on.
        return RunResult(tuple(argv), 127, str(missing))
    output = (completed.stdout or "") + (completed.stderr or "")
    return RunResult(tuple(argv), completed.returncode, output[-LIMIT:])


def _text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    return output.decode("utf-8", "replace") if isinstance(output, bytes) else output
