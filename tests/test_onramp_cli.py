"""D5.5: the `doctor` and `init` CLI surface.

Both are adoption commands, so both have to be usable before anything is set
up — including before a token or a network connection exists. That is why
each has an offline form, and since D14 both have the same one: without
`--repo` each does the whole of the half that needs no credential.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.cli import main

pytestmark = pytest.mark.unit


def tree(root: Path) -> None:
    """A repository as `init` would leave it, minus the labels.

    Two paths, since D13: a config and a directory of graphs. Nothing under
    `.github/workflows/`, because nothing runs there any more.
    """
    (root / ".github").mkdir(parents=True)
    (root / ".github" / "dispatchkit.toml").write_text("[caps]\ncloud = 3\n", encoding="utf-8")
    (root / "docs" / "plans").mkdir(parents=True)


class TestDoctorOffline:
    def test_it_checks_the_working_tree_without_a_repository(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tree(tmp_path)
        assert main(["doctor", "--root", str(tmp_path)]) == 0

        out = capsys.readouterr().out
        assert "ok   plans" in out
        assert "the repository was not inspected" in out

    def test_a_bare_tree_fails_and_names_what_is_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["doctor", "--root", str(tmp_path)]) == 1

        out = capsys.readouterr().out
        assert "FAIL plans" in out
        assert "mkdir" in out

    def test_it_never_reaches_the_network_without_a_repository(self, tmp_path: Path) -> None:
        # The `unit` tier blocks sockets outright, so this passing at all is
        # the assertion: no client is constructed.
        tree(tmp_path)
        assert main(["doctor", "--root", str(tmp_path)]) == 0

    def test_a_config_in_the_old_root_location_is_still_found(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tree(tmp_path)
        (tmp_path / ".github" / "dispatchkit.toml").unlink()
        (tmp_path / "dispatchkit.toml").write_text("[caps]\ncloud = 1\n", encoding="utf-8")

        assert main(["doctor", "--root", str(tmp_path)]) == 0
        assert "dispatchkit.toml" in capsys.readouterr().out

    def test_the_configured_plans_directory_is_the_one_checked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tree(tmp_path)
        (tmp_path / ".github" / "dispatchkit.toml").write_text(
            '[paths]\nplans = "graphs"\n', encoding="utf-8"
        )

        assert main(["doctor", "--root", str(tmp_path)]) == 1
        assert "graphs" in capsys.readouterr().out

    def test_a_broken_config_is_reported_as_unusable_not_as_unhealthy(self, tmp_path: Path) -> None:
        tree(tmp_path)
        (tmp_path / ".github" / "dispatchkit.toml").write_text("caps = [\n", encoding="utf-8")
        assert main(["doctor", "--root", str(tmp_path)]) == 1


class TestInitOffline:
    def test_without_a_repository_it_writes_the_half_that_needs_no_token(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Files and directories need no credential. D14 removed the `--local`
        # flag that used to gate this: the split was really at the `project`
        # scope, and that scope is gone.
        assert main(["init", "--root", str(tmp_path)]) == 0

        assert (tmp_path / ".github" / "dispatchkit.toml").exists()
        assert (tmp_path / "docs" / "plans").is_dir()

        out = capsys.readouterr().out
        assert "CreateLabel dispatchkit" in out
        assert "no --repo, so the labels were not created" in out

    def test_it_plans_no_board_field(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["init", "--root", str(tmp_path)])
        out = capsys.readouterr().out
        assert "CreateField" not in out
        assert "SetFieldOptions" not in out

    def test_it_is_idempotent(self, tmp_path: Path) -> None:
        main(["init", "--root", str(tmp_path)])
        written = (tmp_path / ".github" / "dispatchkit.toml").read_text(encoding="utf-8")

        assert main(["init", "--root", str(tmp_path)]) == 0
        assert (tmp_path / ".github" / "dispatchkit.toml").read_text(encoding="utf-8") == written

    def test_what_it_writes_satisfies_doctor(self, tmp_path: Path) -> None:
        """The on-ramp's whole promise, as one assertion."""
        main(["init", "--root", str(tmp_path)])
        assert main(["doctor", "--root", str(tmp_path)]) == 0

    def test_it_never_reaches_the_network_without_a_repository(self, tmp_path: Path) -> None:
        # The `unit` tier blocks sockets outright, so this passing at all is
        # the assertion: no client is constructed.
        assert main(["init", "--root", str(tmp_path)]) == 0

    def test_a_relative_root_writes_where_it_says_it_does(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: the root was joined twice, once when the facts were
        # gathered and again when the tree was written, so a relative root
        # landed everything under `<root>/<root>/` while the summary named
        # `<root>/`. Absolute roots hid it, and every other test uses one.
        monkeypatch.chdir(tmp_path)
        assert main(["init", "--root", "sub"]) == 0

        assert (tmp_path / "sub" / ".github" / "dispatchkit.toml").exists()
        assert not (tmp_path / "sub" / "sub").exists()

    def test_a_relative_root_converges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        main(["init", "--root", "sub"])
        capsys.readouterr()

        assert main(["init", "--root", "sub"]) == 0
        assert main(["doctor", "--root", "sub"]) == 0
        assert "init: 0 file(s), 0 directory(ies)" in capsys.readouterr().out


class TestRepositoryUnreachable:
    """`gh` failing is the diagnosis, not a crash.

    Every first-adopter failure `doctor` exists to name — a repository that
    does not exist, a name typed wrong, no credential at all — reaches the
    adapter as a non-zero `gh` exit. That must come back as a reported failure
    and exit 2 ("could not be carried out"), never a traceback.
    """

    class Unreachable:
        def __init__(self, **_: object) -> None:
            pass

        def token_scopes(self) -> tuple[str, ...] | None:
            return None

        def agent_available(self) -> bool:
            raise RuntimeError("gh api failed: repository not found")

        def fetch_labels(self) -> tuple[str, ...]:
            raise RuntimeError("gh api failed: repository not found")

    def test_doctor_reports_it_and_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree(tmp_path)
        monkeypatch.setattr("dispatchkit.cli.GhCli", self.Unreachable)

        code = main(["doctor", "--root", str(tmp_path), "--repo", "o/n"])

        assert code == 2
        out = capsys.readouterr().out
        assert "FAIL repository" in out
        assert "repository not found" in out
        assert "ok   plans" in out  # the local half still reported

    def test_init_reports_it_and_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree(tmp_path)
        monkeypatch.setattr("dispatchkit.cli.GhCli", self.Unreachable)

        code = main(["init", "--root", str(tmp_path), "--repo", "o/n"])

        assert code == 2
        assert "repository not found" in capsys.readouterr().err


class TestWatchCannotReachGitHub:
    """A pass that cannot read the repository must say so, not traceback.

    This is the scheduler's most likely first failure, and it was found by
    running it: with no token the pass died with a bare `RuntimeError` stack,
    twelve frames deep, ending in a `gh` message about a missing credential.
    `init` and `doctor` already caught this; the pass was the one path that
    did not, so the failure adopters are most likely to hit was presented
    worst. It matters more now than it did under a cron: `watch` shows the
    failure to a person who is watching, and the first thing they read must
    be the thing to fix.
    """

    class Unreachable:
        def __init__(self, *, repo: str) -> None:
            self.repo = repo

        def fetch_state(self) -> object:
            raise RuntimeError(
                "gh api failed: gh: To use GitHub CLI in a GitHub Actions "
                "workflow, set the GH_TOKEN environment variable."
            )

    def test_it_reports_the_failure_and_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree(tmp_path)
        monkeypatch.setattr("dispatchkit.cli.GhCli", self.Unreachable)

        code = main(
            [
                "watch",
                "--once",
                "--push",
                "--repo",
                "o/n",
                "--config",
                str(tmp_path / ".github" / "dispatchkit.toml"),
            ]
        )

        assert code == 2
        err = capsys.readouterr().err
        assert "cannot read" in err
        assert "GH_TOKEN" in err

    def test_it_does_not_raise(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree(tmp_path)
        monkeypatch.setattr("dispatchkit.cli.GhCli", self.Unreachable)
        # The whole point: a missing token is a configuration problem, and a
        # stack trace tells the reader nothing they can act on.
        main(
            [
                "watch",
                "--once",
                "--push",
                "--repo",
                "o/n",
                "--config",
                str(tmp_path / ".github" / "dispatchkit.toml"),
            ]
        )
