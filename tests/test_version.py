"""D17: the version has to be sayable, and there has to be one of it.

The machine block is a wire format written into other people's issues, parsed
by a closed grammar that refuses unknown keys. That refusal is correct, and it
is also exactly what an adopter hits when their operator runs an older
dispatchkit against issues a newer one wrote -- with the symptom being a parse
error on a body that looks perfectly fine to a human. None of that is
diagnosable until the tool can say what it is.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from dispatchkit.version import __version__, at_least, parse_version

pytestmark = pytest.mark.unit


class TestThereIsOneVersion:
    def test_the_package_declares_it(self) -> None:
        assert parse_version(__version__) is not None

    def test_the_build_metadata_reads_the_same_file(self) -> None:
        """Two hard-coded versions is the drift this deliverable is about.

        A pin, a stamp and a `--version` flag that disagree with the wheel are
        worse than none of them, because each is evidence for a different
        answer. `pyproject.toml` therefore declares the version dynamic and
        points hatchling at `version.py`, so there is one string.
        """
        document = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        )

        assert "version" in document["project"].get("dynamic", [])
        assert document["project"].get("version") is None
        assert document["tool"]["hatch"]["version"]["path"] == "src/dispatchkit/version.py"

    def test_the_installed_distribution_agrees(self) -> None:
        """Belt and braces: the wheel in this environment says the same thing."""
        from importlib.metadata import version

        assert version("dispatchkit") == __version__


class TestParsing:
    def test_it_reads_the_usual_shape(self) -> None:
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_a_short_version_is_padded(self) -> None:
        """`requires_version = "0.3"` means 0.3.0, which is what a human means."""
        assert parse_version("0.3") == (0, 3, 0)

    def test_nonsense_is_none_rather_than_an_exception(self) -> None:
        """The pin comes from an adopter's config file, so it is user input.

        A bad pin must be reportable as a lint beside every other config
        problem, in the one pass this codebase reports problems in -- not a
        traceback out of the middle of a doctor run.
        """
        for bad in ("", "latest", "1.x", "v1.2.3", "1.2.3.4", "-1.0.0", "1..2"):
            assert parse_version(bad) is None, bad


class TestComparison:
    def test_the_same_version_satisfies_its_own_pin(self) -> None:
        assert at_least("0.3.0", "0.3.0") is True

    def test_a_newer_tool_satisfies_an_older_pin(self) -> None:
        assert at_least("0.4.0", "0.3.9") is True
        assert at_least("1.0.0", "0.9.9") is True

    def test_an_older_tool_does_not(self) -> None:
        assert at_least("0.2.0", "0.3.0") is False

    def test_it_compares_numerically_not_lexically(self) -> None:
        """`"0.10.0" < "0.9.0"` as strings, and that is a silent wrong answer."""
        assert at_least("0.10.0", "0.9.0") is True
        assert at_least("0.9.0", "0.10.0") is False

    def test_an_unparseable_side_is_not_an_answer(self) -> None:
        """`None`, never a default.

        Guessing "satisfied" hides a broken pin; guessing "unsatisfied" stops
        a working install over a typo. The caller has to decide, and it has
        the context to report it as the config error it is.
        """
        assert at_least("0.3.0", "nonsense") is None
        assert at_least("nonsense", "0.3.0") is None


class TestTheFlag:
    def test_the_cli_prints_it_and_exits_zero(self) -> None:
        """Run as a subprocess, because `--version` exits the process.

        It also has to work *without* a subcommand, and the parser requires
        one -- which is the thing worth proving rather than assuming.
        """
        result = subprocess.run(
            [sys.executable, "-m", "dispatchkit", "--version"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert __version__ in result.stdout
