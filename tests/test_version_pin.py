"""D17: an adopter states which dispatchkit wrote their issues.

`block.py` refuses unknown keys, which is right, and which means an operator
on an older dispatchkit hits a parse error on a body a human would call fine.
The pin turns that into a sentence: `doctor` goes red, names both versions and
says what to run. Fails closed, at a human, before anything is written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.config import load_config
from dispatchkit.doctor import LocalFacts, check_local, check_version
from dispatchkit.errors import GraphError
from dispatchkit.init import config_template
from dispatchkit.version import __version__

pytestmark = pytest.mark.unit


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "dispatchkit.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestTheKey:
    def test_it_is_absent_by_default(self, tmp_path: Path) -> None:
        """A pin is opt-in. An adopter who has not been bitten owes nothing."""
        assert load_config(_config(tmp_path, "")).requires_version is None

    def test_it_is_read(self, tmp_path: Path) -> None:
        config = load_config(_config(tmp_path, 'requires_version = "0.3"\n'))

        assert config.requires_version == "0.3"

    def test_a_pin_that_is_not_a_version_is_a_config_issue(self, tmp_path: Path) -> None:
        """Reported in the same pass as every other config problem, not raised
        out of the middle of a `doctor` run."""
        with pytest.raises(GraphError) as caught:
            load_config(_config(tmp_path, 'requires_version = "latest"\n'))

        assert [issue.code for issue in caught.value.issues] == ["invalid-version"]

    def test_a_pin_that_is_not_a_string_is_a_config_issue(self, tmp_path: Path) -> None:
        with pytest.raises(GraphError) as caught:
            load_config(_config(tmp_path, "requires_version = 0.3\n"))

        assert [issue.code for issue in caught.value.issues] == ["invalid-type"]


class TestTheCheck:
    def test_with_no_pin_it_reports_the_version_and_passes(self) -> None:
        """Still worth a line. An operator asking what they are running is the
        first thing every other question depends on."""
        check = check_version(None)

        assert check.ok
        assert __version__ in check.detail

    def test_a_satisfied_pin_names_both(self) -> None:
        check = check_version("0.1.0")

        assert check.ok
        assert "0.1.0" in check.detail
        assert __version__ in check.detail

    def test_an_unsatisfied_pin_fails_and_says_how_to_fix_it(self) -> None:
        check = check_version("99.0.0")

        assert not check.ok
        assert "99.0.0" in check.detail
        assert __version__ in check.detail
        assert "uv tool install" in check.remedy

    def test_an_unparseable_pin_fails_rather_than_being_ignored(self) -> None:
        """`at_least` returns `None` and refuses to guess; so does this.

        A pin nobody can read is a pin nobody is protected by, and passing
        silently would be the reassuring wrong answer.
        """
        check = check_version("nonsense")

        assert not check.ok

    def test_it_is_part_of_the_offline_checks(self) -> None:
        """It needs no credential, so it runs for an adopter who has not
        authenticated yet -- which is exactly when it is most useful."""
        checks = check_local(
            LocalFacts(
                config_path=Path("dispatchkit.toml"),
                config_exists=False,
                plans=Path("docs/plans"),
                plans_exists=True,
            )
        )

        assert "version" in [check.name for check in checks]


class TestWhatInitWrites:
    """The pin is written at bootstrap, to the version doing the bootstrapping.

    That is the zero-friction reading: a newer tool always satisfies an older
    floor, so the pin never obstructs an upgrade. It goes red only on a
    *downgrade* below the version that wrote the repository's issues, which is
    the failure it exists to name.
    """

    def test_the_template_pins_the_running_version(self) -> None:
        text = config_template()

        assert f'requires_version = "{__version__}"' in text

    def test_the_template_it_writes_is_a_config_this_tool_accepts(
        self, tmp_path: Path
    ) -> None:
        """The template is a file an adopter is handed; it must round-trip.

        `init` has written a config with a key `load_config` rejects before,
        and the only thing that catches it is asking the parser.
        """
        config = load_config(_config(tmp_path, config_template()))

        assert config.requires_version == __version__

    def test_and_doctor_is_then_happy_with_it(self, tmp_path: Path) -> None:
        assert check_version(load_config(_config(tmp_path, config_template())).requires_version).ok
