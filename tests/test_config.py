"""D4: `dispatchkit.toml` parsing.

The caps are the one knob that changes how much parallel work exists, so a
typo'd key silently reverting to a default is exactly the failure this schema
is closed to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.config import DEFAULT_LOCATIONS, SchedulerConfig, find_config, load_config
from dispatchkit.errors import GraphError
from dispatchkit.model import Lane


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "dispatchkit.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_a_missing_file_yields_the_documented_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "absent.toml")
        assert (config.cap(Lane.CLOUD), config.cap(Lane.LOCAL)) == (3, 1)
        assert config.retry_budget == 3

    def test_an_empty_file_yields_the_same_defaults(self, tmp_path: Path) -> None:
        assert load_config(write(tmp_path, "")) == SchedulerConfig()

    def test_a_partial_file_keeps_the_defaults_it_does_not_mention(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, "[caps]\nlocal = 2\n"))
        assert (config.cap(Lane.CLOUD), config.cap(Lane.LOCAL)) == (3, 2)


class TestOverrides:
    def test_caps_and_budget_are_read(self, tmp_path: Path) -> None:
        text = "[caps]\ncloud = 5\nlocal = 0\n\n[retry]\nbudget = 2\n"
        config = load_config(write(tmp_path, text))
        assert (config.cap(Lane.CLOUD), config.cap(Lane.LOCAL)) == (5, 0)
        assert config.retry_budget == 2

    def test_a_zero_cap_pauses_a_lane_without_editing_the_graph(self, tmp_path: Path) -> None:
        assert load_config(write(tmp_path, "[caps]\ncloud = 0\n")).cap(Lane.CLOUD) == 0

    def test_an_unconfigured_lane_admits_nothing(self) -> None:
        # Fail closed: a lane nobody has sized is not implicitly unlimited.
        assert SchedulerConfig(caps={Lane.CLOUD: 3}).cap(Lane.LOCAL) == 0


class TestLocation:
    """`.github/dispatchkit.toml` is the home; the repo root is the fallback.

    The config claims a filename in somebody else's tree, so it belongs in the
    directory already reserved for tooling. The root location keeps working
    because the first repositories to run this put it there.
    """

    def test_the_github_directory_is_the_preferred_home(self, tmp_path: Path) -> None:
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "dispatchkit.toml").write_text("", encoding="utf-8")
        assert find_config(tmp_path) == tmp_path / ".github" / "dispatchkit.toml"

    def test_a_root_config_is_still_found(self, tmp_path: Path) -> None:
        (tmp_path / "dispatchkit.toml").write_text("", encoding="utf-8")
        assert find_config(tmp_path) == tmp_path / "dispatchkit.toml"

    def test_the_github_directory_wins_when_both_exist(self, tmp_path: Path) -> None:
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "dispatchkit.toml").write_text("", encoding="utf-8")
        (tmp_path / "dispatchkit.toml").write_text("", encoding="utf-8")
        assert find_config(tmp_path).parent.name == ".github"

    def test_with_no_config_at_all_it_names_where_one_should_go(self, tmp_path: Path) -> None:
        assert find_config(tmp_path) == tmp_path / DEFAULT_LOCATIONS[0]

    def test_this_repository_keeps_its_config_where_it_says_to(self) -> None:
        root = Path(__file__).resolve().parents[1]
        assert find_config(root) == root / ".github" / "dispatchkit.toml"


class TestPaths:
    def test_the_plans_directory_is_configurable(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, '[paths]\nplans = "plans"\n'))
        assert config.graph_path("refactor") == Path("plans/refactor.tasks.toml")

    def test_the_convention_is_the_default(self, tmp_path: Path) -> None:
        assert load_config(tmp_path / "absent.toml").graph_path("refactor") == Path(
            "docs/plans/refactor.tasks.toml"
        )


class TestFence:
    """The blast-radius fence: what the pipeline may never merge unattended.

    Auto-merge (D9) consults this. It lives in config because an adopter's
    dangerous paths are theirs, not ours — and it defaults to covering *this*
    tool's own inputs, which is the case a hardcoded list got wrong before.
    """

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            ".github/workflows/nested/thing.yml",
            ".github/dispatchkit.toml",
            "docs/plans/refactor.tasks.toml",
        ],
    )
    def test_the_defaults_cover_the_pipelines_own_rules(self, path: str) -> None:
        assert SchedulerConfig().is_fenced(path)

    def test_ordinary_source_is_not_fenced(self) -> None:
        assert not SchedulerConfig().is_fenced("src/dispatchkit/model.py")

    def test_the_fence_covers_the_config_file_it_was_loaded_from(self, tmp_path: Path) -> None:
        # The bug this replaces: the fence named `dispatch.toml`, a filename
        # the config no longer had, so it did not cover itself.
        path = tmp_path / "dispatchkit.toml"
        path.write_text("", encoding="utf-8")
        assert load_config(path).is_fenced("dispatchkit.toml")

    def test_moving_the_plans_directory_moves_the_fence_with_it(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, '[paths]\nplans = "plans"\n'))
        assert config.is_fenced("plans/refactor.tasks.toml")
        assert not config.is_fenced("docs/plans/refactor.tasks.toml")

    def test_an_explicit_fence_replaces_the_defaults_rather_than_adding_to_them(
        self, tmp_path: Path
    ) -> None:
        config = load_config(write(tmp_path, '[fence]\npaths = ["infra/**"]\n'))
        assert config.is_fenced("infra/main.tf")
        assert not config.is_fenced(".github/workflows/ci.yml")

    def test_an_empty_fence_is_allowed_because_it_is_a_stated_choice(self, tmp_path: Path) -> None:
        assert not load_config(write(tmp_path, "[fence]\npaths = []\n")).is_fenced(
            ".github/workflows/ci.yml"
        )

    def test_a_leading_dot_slash_does_not_smuggle_a_path_past_the_fence(self) -> None:
        assert SchedulerConfig().is_fenced("./.github/workflows/ci.yml")


class TestRejections:
    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("[cpas]\ncloud = 3\n", "unknown-key"),
            ('[paths]\nplan = "x"\n', "unknown-key"),
            ("[paths]\nplans = 3\n", "invalid-type"),
            ('[paths]\nplans = "/etc"\n', "invalid-type"),
            ("[fence]\npath = []\n", "unknown-key"),
            ('[fence]\npaths = "x"\n', "invalid-type"),
            ("[fence]\npaths = [3]\n", "invalid-type"),
            ("[retry]\nbudgett = 3\n", "unknown-key"),
            ("[caps]\ncoud = 3\n", "invalid-enum"),
            ("[caps]\ncloud = -1\n", "invalid-type"),
            ('[caps]\ncloud = "3"\n', "invalid-type"),
            ("[caps]\ncloud = true\n", "invalid-type"),
            ("[retry]\nbudget = 0\n", "invalid-type"),
            ("caps = [\n", "invalid-toml"),
        ],
    )
    def test_bad_configuration_is_refused(self, tmp_path: Path, text: str, code: str) -> None:
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, text))
        assert caught.value.issues[0].code == code

    def test_every_problem_is_reported_at_once(self, tmp_path: Path) -> None:
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, "[caps]\ncoud = 3\n\n[retry]\nbudget = 0\n"))
        assert len(caught.value.issues) == 2
