"""D6.6: the shipped default model is one the runner will actually accept.

The local lane's second live run reached the agent and died there:

    Error: Model "claude-opus-5" from --model flag is not available.

`claude-opus-5` is `DEFAULT_MODELS[0]`, so it is what every repository gets
until somebody edits the config — and `doctor --local` had reported the runner
`ok`, because it checks that the `copilot` binary exists and not that the argv
it will be handed is one the binary accepts.

A default is a promise made to an account the author cannot see. Model
availability varies by plan, by org policy and by month, so no specific name
can keep that promise; `auto` is the CLI's own documented way to say "pick one
that works", which is exactly what a default means. Pinning a model stays
available to anyone who wants reproducibility, and `models` still lists the
specific names a task may choose between — that allowlist is the trust
boundary for agent-authored graphs and is unchanged.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from dispatchkit.config import DEFAULT_MODELS, RunnerConfig, load_config
from dispatchkit.init import CONFIG_TEMPLATE

pytestmark = pytest.mark.unit

#: What the `copilot` CLI documents for "let Copilot pick automatically".
AUTO = "auto"


class TestTheDefault:
    def test_it_is_auto(self) -> None:
        assert RunnerConfig().model == AUTO

    def test_auto_is_allowed_by_the_default_allowlist(self) -> None:
        # Otherwise the default config fails its own `runner.model` check.
        assert RunnerConfig().allows(AUTO)

    def test_the_specific_names_are_still_offered(self) -> None:
        # `auto` replaces the default, not the choice. A task that needs a
        # particular model can still name one.
        assert len(DEFAULT_MODELS) > 1


class TestTheTemplateAgrees:
    """`init` writes the template; the parser reads the defaults. Two
    statements of one value are two values, eventually."""

    def test_the_template_names_the_same_default(self) -> None:
        runner = tomllib.loads(CONFIG_TEMPLATE)["runner"]
        assert runner["model"] == RunnerConfig().model

    def test_the_template_offers_the_same_allowlist(self) -> None:
        runner = tomllib.loads(CONFIG_TEMPLATE)["runner"]
        assert tuple(runner["models"]) == DEFAULT_MODELS


class TestNarrowingTheAllowlist:
    """Making `auto` the global default must not trap somebody who pins.

    Setting `models` to a specific list and leaving `model` alone is the
    obvious way to say "only these" — and it would have become an error the
    moment the global default stopped being a member of every list. So an
    explicit `models` supplies the default too: the model used when a task
    names none comes from the list you actually provided.
    """

    def _config(self, tmp_path: Path, body: str) -> RunnerConfig:
        path = tmp_path / "dispatchkit.toml"
        path.write_text(body, encoding="utf-8")
        return load_config(path).runner

    def test_an_explicit_list_supplies_the_default(self, tmp_path: Path) -> None:
        runner = self._config(tmp_path, '[runner]\nmodels = ["claude-sonnet-5"]\n')
        assert runner.model == "claude-sonnet-5"

    def test_an_explicit_model_still_wins(self, tmp_path: Path) -> None:
        runner = self._config(
            tmp_path,
            '[runner]\nmodel = "claude-opus-5"\nmodels = ["claude-opus-5", "claude-sonnet-5"]\n',
        )
        assert runner.model == "claude-opus-5"

    def test_an_empty_list_still_forbids_choosing(self, tmp_path: Path) -> None:
        # `models = []` means "a task may not pick", not "anything goes", and
        # it cannot supply a default because it names nothing.
        runner = self._config(tmp_path, "[runner]\nmodels = []\n")
        assert runner.model == DEFAULT_MODELS[0]
        assert not runner.allows("claude-opus-5")
