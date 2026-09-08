"""D4: `dispatchkit.toml` parsing.

The caps are the one knob that changes how much parallel work exists, so a
typo'd key silently reverting to a default is exactly the failure this schema
is closed to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.config import SchedulerConfig, load_config
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


class TestRejections:
    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("[cpas]\ncloud = 3\n", "unknown-key"),
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
