"""D17: the two commands that carry the contract out of this repository.

`schema` prints the reference; `skill` prints or installs the planning skill.
Both exist because an adopter has a wheel and not a checkout, and a reference
they cannot read is not a reference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.cli import main
from dispatchkit.doctor import LocalFacts, check_local, check_skill
from dispatchkit.skill import DEFAULT_SKILL_PATH, STAMP_PREFIX, skill_text
from dispatchkit.version import __version__

pytestmark = pytest.mark.unit


class TestSchema:
    def test_it_prints_the_reference(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["schema"]) == 0
        assert "[[task]]" in capsys.readouterr().out


class TestSkillPrinting:
    def test_print_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["skill", "--print", "--root", str(tmp_path)]) == 0

        assert "dispatchkit-planning" in capsys.readouterr().out
        assert list(tmp_path.iterdir()) == []

    def test_printing_is_the_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The safe half of a command that can also write to somebody's tree."""
        assert main(["skill"]) == 0
        assert "dispatchkit-planning" in capsys.readouterr().out


class TestSkillInstalling:
    def test_it_writes_the_skill_and_its_directory(self, tmp_path: Path) -> None:
        assert main(["skill", "--install", "--root", str(tmp_path)]) == 0

        written = tmp_path / DEFAULT_SKILL_PATH
        assert written.exists()
        assert f"{STAMP_PREFIX}{__version__}" in written.read_text()

    def test_it_overwrites_an_older_one(self, tmp_path: Path) -> None:
        """Upgrading is the whole reason this is a command and not just an
        `init` step: `init` runs once, and the skill is rewritten whenever the
        tool moves."""
        written = tmp_path / DEFAULT_SKILL_PATH
        written.parent.mkdir(parents=True)
        written.write_text("stale\n", encoding="utf-8")

        assert main(["skill", "--install", "--root", str(tmp_path)]) == 0
        assert written.read_text() == skill_text()

    def test_installing_twice_changes_nothing_the_second_time(self, tmp_path: Path) -> None:
        """The convergence standard every mutating thing here is held to."""
        main(["skill", "--install", "--root", str(tmp_path)])
        first = (tmp_path / DEFAULT_SKILL_PATH).read_text()

        main(["skill", "--install", "--root", str(tmp_path)])

        assert (tmp_path / DEFAULT_SKILL_PATH).read_text() == first

    def test_an_explicit_path_is_honoured(self, tmp_path: Path) -> None:
        """`.github/skills/` is a convention, not a standard; a runtime that
        looks elsewhere should not need a fork of the tool."""
        target = tmp_path / "elsewhere" / "SKILL.md"

        assert main(["skill", "--install", "--path", str(target)]) == 0
        assert target.exists()


class TestInitInstallsIt:
    def test_the_plan_includes_the_skill(self, tmp_path: Path) -> None:
        """An adopter should not have to know a second command exists to get
        the thing that teaches an agent to use the first one."""
        from dispatchkit.init import plan_init

        plan = plan_init(
            [],
            LocalFacts(
                config_path=tmp_path / "dispatchkit.toml",
                config_exists=True,
                plans=tmp_path / "docs" / "plans",
                plans_exists=True,
            ),
        )

        assert any(str(DEFAULT_SKILL_PATH) in str(op) for op in plan.operations)


class TestDoctorNoticesAStaleSkill:
    def test_no_skill_is_not_a_problem(self, tmp_path: Path) -> None:
        """Installing it is opt-in, and a repository planned elsewhere never
        needs one."""
        assert check_skill(None).ok

    def test_a_current_stamp_passes(self) -> None:
        assert check_skill(__version__).ok

    def test_a_stamp_behind_the_tool_fails_with_the_command_to_fix_it(self) -> None:
        check = check_skill("0.0.1")

        assert not check.ok
        assert "0.0.1" in check.detail
        assert "dispatchkit skill --install" in check.remedy

    def test_a_stamp_ahead_of_the_tool_fails_too(self) -> None:
        """The more dangerous direction, and the one a stamp alone would miss.

        A skill newer than the tool means somebody downgraded, or two people
        are running different versions against one repository -- and the skill
        may be teaching a format this binary cannot parse.
        """
        assert not check_skill("99.0.0").ok

    def test_an_unreadable_stamp_is_reported_rather_than_ignored(self) -> None:
        assert not check_skill("nonsense").ok

    def test_it_runs_offline_with_the_other_local_checks(self, tmp_path: Path) -> None:
        skill = tmp_path / DEFAULT_SKILL_PATH
        skill.parent.mkdir(parents=True)
        skill.write_text(f"{STAMP_PREFIX}0.0.1 -->\n", encoding="utf-8")

        checks = check_local(
            LocalFacts(
                config_path=tmp_path / "dispatchkit.toml",
                config_exists=False,
                plans=tmp_path / "docs" / "plans",
                plans_exists=True,
                skill_stamp="0.0.1",
            )
        )

        assert [check.ok for check in checks if check.name == "skill"] == [False]
