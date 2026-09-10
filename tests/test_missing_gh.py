"""D11: `gh` is the one hard external dependency, so its absence is a diagnosis.

Everything that touches GitHub goes through `gh_cli`, which shells out to an
argv list starting with `gh`. If that program is not on PATH, `subprocess.run`
raises `FileNotFoundError` — which nothing catches, because every caller that
handles adapter failure handles `RuntimeError`.

So the failure came out of `dispatchkit doctor` as a traceback. That is the
one command whose entire job is to explain a broken setup, and it was the
command least able to survive the most basic thing being broken.

Two halves. The adapter translates, so no command tracebacks and each one
degrades to the "cannot read the repository" path it already has. And `doctor`
gets an explicit check, so the answer is a named remedy rather than a
plausible-looking guess about the repository, which is what the translated
error alone would read as.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from dispatchkit.cli import main
from dispatchkit.doctor import check_cli

pytestmark = pytest.mark.unit

MISSING = FileNotFoundError(2, "No such file or directory", "gh")
STATE = Path(__file__).resolve().parent / "fixtures" / "live_state.json"


@contextmanager
def absent() -> Iterator[None]:
    """`gh` is not installed: neither the lookup nor the spawn can find it."""
    with (
        patch.object(shutil, "which", return_value=None),
        patch.object(subprocess, "run", side_effect=MISSING),
    ):
        yield


class TestTheCheck:
    def test_a_program_that_is_there_passes(self) -> None:
        check = check_cli("gh", found="/opt/homebrew/bin/gh")

        assert check.ok
        assert "/opt/homebrew/bin/gh" in check.detail

    def test_a_program_that_is_missing_fails(self) -> None:
        check = check_cli("gh", found=None)

        assert not check.ok
        assert check.name == "gh"

    def test_the_remedy_says_how_to_get_it(self) -> None:
        assert "cli.github.com" in check_cli("gh", found=None).remedy


class TestNoCommandTracebacks:
    """The adapter translates, so the existing `RuntimeError` handling catches
    it. Each command already knows how to report that it could not read the
    repository; none of them knew how to survive `gh` not existing."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["doctor", "--repo", "o/n"],
            ["init", "--repo", "o/n"],
            ["watch", "--once", "--repo", "o/n"],
        ],
    )
    def test_it_exits_rather_than_raising(self, argv: list[str]) -> None:
        with patch.object(subprocess, "run", side_effect=MISSING):
            assert main(argv) != 0

    def test_a_dry_run_against_a_snapshot_never_needed_it(self, tmp_path: object) -> None:
        # The offline path is offline. Worth pinning: it is the reason the
        # whole test suite runs without a credential.
        with patch.object(subprocess, "run", side_effect=MISSING):
            assert main(["watch", "--once", "--state", str(STATE)]) == 0

    def test_doctor_names_gh_rather_than_blaming_the_repository(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Without the explicit check this surfaces as "cannot read o/n", which
        # sends somebody to look at a repository that is perfectly fine.
        with absent():
            main(["doctor", "--repo", "o/n"])

        out = capsys.readouterr().out
        assert "FAIL gh" in out
        assert "cli.github.com" in out


class TestTheRemoteChecksAreSkippedWithoutIt:
    def test_doctor_does_not_pretend_to_have_read_the_repository(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A `gh` that is absent answered nothing, so reporting `token-scopes`
        # or `labels` either way would be an invention.
        with absent():
            main(["doctor", "--repo", "o/n"])

        out = capsys.readouterr().out
        assert "token-scopes" not in out
        assert "labels" not in out

    def test_the_local_checks_still_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        # They need nothing from the network, and somebody with no `gh` still
        # deserves to be told their fence is open.
        with absent():
            main(["doctor", "--repo", "o/n"])

        assert "fence" in capsys.readouterr().out
