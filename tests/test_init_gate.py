"""D11: `init` does not report success over a check it could not satisfy.

`init` creates what is missing and, deliberately, never overwrites what is
already there — somebody else's config is not ours to rewrite. Those two rules
combine into a hole: run `init` in a repository whose existing config has a
fence gap and it steps over the file, prints `init: 0 file(s)` and exits 0,
while `doctor` on the very same tree exits 1 on a check about whether an
unattended merge can rewrite the pipeline's own rules.

Setting a repository up is exactly when somebody decides they are finished, so
that is the worst possible moment to be quiet. The gate is a postcondition
rather than a precondition: do the idempotent work first — the labels and the
plans directory are a gain whatever else is wrong — then re-read the tree and
run the local checks against what is actually there now.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from dispatchkit.cli import main

pytestmark = pytest.mark.unit


def repository(tmp_path: Path) -> Path:
    (tmp_path / ".github").mkdir()
    return tmp_path


FENCE_GAP = '[fence]\npaths = ["src/**"]\n'


class TestAFailingCheckIsNotASuccess:
    def test_a_fence_gap_in_an_existing_config_fails_init(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = repository(tmp_path)
        (root / ".github" / "dispatchkit.toml").write_text(FENCE_GAP, encoding="utf-8")

        assert main(["init", "--root", str(root)]) != 0

    def test_it_says_which_check_and_what_to_do(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = repository(tmp_path)
        (root / ".github" / "dispatchkit.toml").write_text(FENCE_GAP, encoding="utf-8")

        main(["init", "--root", str(root)])

        output = capsys.readouterr()
        assert "fence" in output.out
        # The remedy, not just the verdict: this is somebody's first minute
        # with the tool.
        assert "fence.paths" in output.out

    def test_the_work_still_happened(self, tmp_path: Path) -> None:
        # A postcondition, not a precondition. The plans directory is a gain
        # whether or not the config beside it is sound, and refusing to create
        # it would mean a second failing check to read through.
        root = repository(tmp_path)
        (root / ".github" / "dispatchkit.toml").write_text(FENCE_GAP, encoding="utf-8")

        main(["init", "--root", str(root)])

        assert (root / "docs" / "plans").is_dir()

    def test_the_existing_config_is_still_not_overwritten(self, tmp_path: Path) -> None:
        # The gate reports the problem; it does not seize the file. Rewriting
        # somebody's config to make a check go green is the one thing worse
        # than the check being red.
        root = repository(tmp_path)
        config = root / ".github" / "dispatchkit.toml"
        config.write_text(FENCE_GAP, encoding="utf-8")

        main(["init", "--root", str(root)])

        assert config.read_text(encoding="utf-8") == FENCE_GAP


class TestGreenOnTheRepositoryItJustSetUp:
    """The check that is red for everybody teaches people to ignore it."""

    def test_a_fresh_repository_ends_healthy(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["init", "--root", str(repository(tmp_path))]) == 0

    def test_the_config_it_writes_passes_its_own_fence_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = repository(tmp_path)
        main(["init", "--root", str(root)])
        capsys.readouterr()

        # Re-running reads the config `init` wrote on the first pass, so the
        # fence check now runs against it rather than against the default.
        assert main(["init", "--root", str(root)]) == 0
        assert "FAIL" not in capsys.readouterr().out

    def test_the_template_it_writes_is_parseable(self, tmp_path: Path) -> None:
        root = repository(tmp_path)
        main(["init", "--root", str(root)])

        tomllib.loads((root / ".github" / "dispatchkit.toml").read_text(encoding="utf-8"))


class TestTheChecksSeeTheWorkThatJustHappened:
    def test_the_plans_directory_is_not_reported_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Re-reading the tree is the whole point: checked against the facts
        # gathered *before* the plan ran, the directory `init` just created
        # would still be reported as absent.
        root = repository(tmp_path)
        (root / ".github" / "dispatchkit.toml").write_text(FENCE_GAP, encoding="utf-8")

        main(["init", "--root", str(root)])

        output = capsys.readouterr().out
        plans = [line for line in output.splitlines() if "plans:" in line]
        assert plans and all(line.startswith("ok") for line in plans), output


class TestTheAdvisoryStepsSurvive:
    """Being red must not swallow what a human still has to do by hand."""

    def test_the_manual_steps_are_still_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = repository(tmp_path)
        (root / ".github" / "dispatchkit.toml").write_text(FENCE_GAP, encoding="utf-8")

        main(["init", "--root", str(root)])

        assert "MANUAL" in capsys.readouterr().out
