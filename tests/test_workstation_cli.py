"""D6.2: the second adapter, and the commands it builds.

`gh_cli.py` was the only module allowed to shell out until now. The local lane
needs git plumbing and one supervised child, which is a different kind of
subprocess with a different threat model — the child is an agent, and it must
not inherit the credential that can write to the repository.

The commands are pure functions so the argv shape can be asserted without a
subprocess anywhere. The parts that cannot be tested offline (actually forking)
are `live`-marked or exercised through the fake.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from dispatchkit.model import TaskId, TaskRef
from dispatchkit.workstation import RunResult, Worktree
from dispatchkit.workstation_cli import (
    _parse_worktrees,
    add_command,
    commits_command,
    fetch_command,
    list_command,
    push_command,
    remove_command,
)

pytestmark = pytest.mark.unit

ROOT = Path("/work")
REF = TaskRef(plan="wordfreq", id=TaskId("tokenise"))


class TestEveryCommandIsAnArgvList:
    """The rule that makes the whole thing safe, asserted rather than trusted.

    A branch name is derived from a `TaskId`, which the grammar constrains, but
    the base ref and the worktree path both come from configuration a human
    edits. One `shell=True` anywhere here would make all of that executable.
    """

    def test_no_command_contains_a_shell(self) -> None:
        commands = [
            fetch_command("origin", "main"),
            add_command(ROOT / "a", "dispatchkit/tokenise/1", "origin/main"),
            remove_command(ROOT / "a"),
            list_command(),
            commits_command(),
            push_command("origin", "dispatchkit/tokenise/1"),
        ]
        for command in commands:
            assert command[0] == "git"
            assert all(isinstance(part, str) for part in command)
            assert not any(part in {"&&", "|", ";", "sh", "-c"} for part in command)

    def test_a_worktree_is_added_detached_from_the_remote_base(self) -> None:
        # From `origin/main`, not from whatever the checkout happens to be on.
        # The parent repository is a working tree a human is using; a local
        # lane run must not inherit their half-finished branch.
        command = add_command(ROOT / "a", "dispatchkit/tokenise/1", "origin/main")
        assert command == [
            "git",
            "worktree",
            "add",
            "-b",
            "dispatchkit/tokenise/1",
            str(ROOT / "a"),
            "origin/main",
        ]

    def test_removal_is_forced_because_the_agent_left_the_tree_dirty(self) -> None:
        assert remove_command(ROOT / "a") == [
            "git",
            "worktree",
            "remove",
            "--force",
            str(ROOT / "a"),
        ]

    def test_the_push_names_the_branch_on_both_sides(self) -> None:
        # Never `HEAD`, never a bare branch name: an explicit refspec cannot be
        # redirected by whatever `push.default` the human has configured.
        assert push_command("origin", "dispatchkit/tokenise/1") == [
            "git",
            "push",
            "--force-with-lease",
            "origin",
            "dispatchkit/tokenise/1:refs/heads/dispatchkit/tokenise/1",
        ]

    def test_commits_are_counted_against_the_remote(self) -> None:
        # Against every `origin` ref rather than a named base: the worktree is
        # cut from whichever branch its plan integrates on (D16).
        assert commits_command() == [
            "git",
            "rev-list",
            "--count",
            "HEAD",
            "--not",
            "--remotes=origin",
        ]


class TestReadingTheWorktreeList:
    """`--porcelain` because the human-readable form is not a wire format.

    Recovery walks this list, so a parse that silently drops an entry loses a
    branch somebody's work is on.
    """

    def test_a_worktree_is_read_back_as_the_task_it_belongs_to(self) -> None:
        output = (
            "worktree /work/wordfreq/tokenise\n"
            "HEAD abc123\n"
            "branch refs/heads/dispatchkit/tokenise/1\n"
            "\n"
        )
        assert _parse_worktrees(output, ROOT) == (
            Worktree(REF, Path("/work/wordfreq/tokenise"), "dispatchkit/tokenise/1"),
        )

    def test_the_main_checkout_is_not_a_worktree_of_ours(self) -> None:
        # It is listed first by `git worktree list`, and it is the human's.
        output = (
            "worktree /home/me/repo\nHEAD abc123\nbranch refs/heads/main\n\n"
            "worktree /work/wordfreq/tokenise\n"
            "HEAD def456\n"
            "branch refs/heads/dispatchkit/tokenise/1\n\n"
        )
        assert [tree.ref for tree in _parse_worktrees(output, ROOT)] == [REF]

    def test_a_detached_worktree_has_no_branch_and_is_still_listed(self) -> None:
        # It cannot be pushed, but it may hold commits, so recovery must see
        # it in order to report it rather than silently delete it.
        output = "worktree /work/wordfreq/tokenise\nHEAD abc123\ndetached\n\n"
        trees = _parse_worktrees(output, ROOT)
        assert len(trees) == 1
        assert trees[0].branch == ""

    def test_nothing_is_not_an_error(self) -> None:
        assert _parse_worktrees("", ROOT) == ()


class TestTheChildIsSupervised:
    def test_a_timeout_is_carried_as_a_duration(self) -> None:
        # Asserted here because `timeout=` on `subprocess` is seconds, and a
        # `timedelta` passed straight in would be a silent type error under a
        # config that never sets it.
        from dispatchkit.workstation_cli import _seconds

        assert _seconds(timedelta(hours=2)) == 7200.0


class TestThePathGitReportsIsNotThePathWeGaveIt:
    """Found by running it: `git worktree list` canonicalises.

    On macOS `/tmp` is a symlink to `/private/tmp`, so a worktree created at
    `/tmp/...` is reported at `/private/tmp/...`. Matching on the literal
    string found nothing, which meant recovery saw an empty machine: retained
    worktrees were never cleared, and every retry would then fail on
    `git worktree add` with the path already in use.
    """

    def test_a_canonicalised_path_is_still_recognised(self, tmp_path: Path) -> None:
        root = _symlinked(tmp_path)
        real = (root / "wordfreq" / "tokenise").resolve()
        output = f"worktree {real}\nHEAD abc\nbranch refs/heads/dispatchkit/tokenise/1\n\n"
        assert [tree.ref for tree in _parse_worktrees(output, root)] == [REF]

    def test_the_path_reported_back_is_the_one_git_will_accept(self, tmp_path: Path) -> None:
        # It is handed straight back to `git worktree remove`, so it has to be
        # what git said, not what we would have written.
        root = _symlinked(tmp_path)
        real = (root / "wordfreq" / "tokenise").resolve()
        output = f"worktree {real}\nHEAD abc\nbranch refs/heads/dispatchkit/tokenise/1\n\n"
        assert _parse_worktrees(output, root)[0].path == real


def _symlinked(tmp_path: Path) -> Path:
    """A root reached through a symlink, which is what `/tmp` is on macOS."""
    actual = tmp_path / "actual"
    (actual / "wordfreq" / "tokenise").mkdir(parents=True)
    link = tmp_path / "work"
    link.symlink_to(actual)
    return link


class TestAWorktreeIsAskedWhatItHolds:
    """`--porcelain` does not say, and it is the only thing recovery asks.

    Found the same way: `_parse_worktrees` left `commits` at its default, so
    every recovered worktree looked empty, and recovery would have discarded
    the commits it exists to preserve -- silently, with no error anywhere.
    """

    def test_the_count_is_filled_in_for_each_worktree(self) -> None:
        from dispatchkit.workstation_cli import CliWorkstation

        listed = (
            "worktree /work/wordfreq/tokenise\n"
            "HEAD abc\nbranch refs/heads/dispatchkit/tokenise/1\n\n"
        )
        seen: list[Path] = []

        class Recording(CliWorkstation):
            def _git(self, command, *, cwd=None):  # type: ignore[no-untyped-def]
                if command[1] == "worktree":
                    return RunResult(tuple(command), 0, listed)
                seen.append(cwd)
                return RunResult(tuple(command), 0, "7\n")

        trees = Recording(root=ROOT).worktrees()
        assert trees[0].commits == 7
        assert seen == [Path("/work/wordfreq/tokenise")]
