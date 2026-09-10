"""D6.1: the runner — what actually runs a local task, and how it is spelled.

Two things the design fixed before the executor exists, both here because they
are pure and neither needs a subprocess to prove.

**`caps.local = 1` is an invariant, not a default.** The local lane is a
capability escape hatch, so "I need more local parallelism" is never a request
for slots — it is evidence the task did not need to be local. The config
*rejects* a larger cap rather than ignoring it, because the error message is a
better carrier for that decision than a paragraph nobody reads.

**The argv template is a closed grammar.** The runner is named as a list, never
a string, and substitution replaces whole elements: `n` template elements
produce exactly `n` argv elements whatever the substituted value contains.
That is the property that matters — an agent-influenced value cannot become two
arguments, let alone a shell fragment — and it is what lets a model name from a
graph file be passed to a CLI at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.config import RunnerConfig, load_config
from dispatchkit.errors import GraphError
from dispatchkit.model import Lane

pytestmark = pytest.mark.unit


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "dispatchkit.toml"
    path.write_text(body, encoding="utf-8")
    return path


def codes(exc: GraphError) -> list[str]:
    return [issue.code for issue in exc.issues]


class TestOneLocalTaskIsAnInvariant:
    def test_the_default_is_one(self) -> None:
        assert load_config(Path("nope.toml")).cap(Lane.LOCAL) == 1

    def test_more_than_one_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, "[caps]\nlocal = 2\n"))
        assert "invariant" in codes(caught.value)[0]

    def test_the_message_says_why_rather_than_only_what(self, tmp_path: Path) -> None:
        # The whole reason this is an error and not a clamp.
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, "[caps]\nlocal = 4\n"))
        message = caught.value.issues[0].message
        assert "requires" in message

    def test_zero_is_allowed(self, tmp_path: Path) -> None:
        # Turning the lane off is a legitimate thing to want; it is only
        # *more* than one that the design refuses.
        assert load_config(write(tmp_path, "[caps]\nlocal = 0\n")).cap(Lane.LOCAL) == 0

    def test_the_cloud_cap_is_still_free(self, tmp_path: Path) -> None:
        assert load_config(write(tmp_path, "[caps]\ncloud = 8\n")).cap(Lane.CLOUD) == 8


class TestTheArgvTemplate:
    def test_a_template_element_holding_a_placeholder_is_substituted(self) -> None:
        runner = RunnerConfig(argv=("agent", "-p", "{prompt}"))
        assert runner.render(prompt="do the thing", model="m") == ["agent", "-p", "do the thing"]

    def test_a_value_with_spaces_stays_one_argument(self) -> None:
        # The property the whole design rests on. Through a shell this would
        # be four arguments; here it is one.
        runner = RunnerConfig(argv=("agent", "-p", "{prompt}"))
        rendered = runner.render(prompt="fix the bug; rm -rf /", model="m")
        assert rendered[2] == "fix the bug; rm -rf /"
        assert len(rendered) == 3

    def test_a_value_that_looks_like_a_flag_is_still_one_argument(self) -> None:
        runner = RunnerConfig(argv=("agent", "-p", "{prompt}"))
        rendered = runner.render(prompt="--yolo --allow-all", model="m")
        assert rendered == ["agent", "-p", "--yolo --allow-all"]

    def test_element_count_never_changes(self) -> None:
        runner = RunnerConfig(argv=("agent", "-p", "{prompt}", "--model", "{model}"))
        assert len(runner.render(prompt="a\nb\nc\0d", model="m")) == len(runner.argv)

    def test_a_placeholder_inside_a_larger_element_still_yields_one(self) -> None:
        runner = RunnerConfig(argv=("agent", "--model={model}"))
        assert runner.render(prompt="p", model="claude-opus-5") == [
            "agent",
            "--model=claude-opus-5",
        ]

    def test_an_unknown_placeholder_is_refused_at_load(self, tmp_path: Path) -> None:
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, '[runner]\nargv = ["agent", "{shell}"]\n'))
        assert codes(caught.value) == ["unknown-placeholder"]

    def test_a_placeholder_with_no_value_is_refused_rather_than_emptied(self) -> None:
        # Dropping the element would change the argument count, and an empty
        # string in its place is a different command from the one written.
        runner = RunnerConfig(argv=("agent", "--effort", "{effort}"))
        with pytest.raises(GraphError) as caught:
            runner.render(prompt="p", model="m")
        assert codes(caught.value) == ["missing-substitution"]

    def test_an_empty_argv_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, "[runner]\nargv = []\n"))
        assert codes(caught.value) == ["invalid-type"]

    def test_a_string_argv_is_refused(self, tmp_path: Path) -> None:
        # The one spelling that would reintroduce a shell.
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, '[runner]\nargv = "agent -p {prompt}"\n'))
        assert codes(caught.value) == ["invalid-type"]


class TestTheModelAllowlist:
    def test_a_listed_model_is_accepted(self, tmp_path: Path) -> None:
        config = load_config(
            write(tmp_path, '[runner]\nmodels = ["claude-opus-5", "claude-sonnet-5"]\n')
        )
        assert config.runner.allows("claude-sonnet-5")

    def test_an_unlisted_one_is_not(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, '[runner]\nmodels = ["claude-opus-5"]\n'))
        assert not config.runner.allows("something-else")

    def test_an_empty_allowlist_permits_no_override(self) -> None:
        # Not "permits everything": a graph file is agent-authorable, and the
        # permissive reading of an unanswered question is the wrong one here.
        assert not RunnerConfig(models=()).allows("claude-opus-5")

    def test_the_default_model_must_itself_be_allowed(self, tmp_path: Path) -> None:
        with pytest.raises(GraphError) as caught:
            load_config(
                write(tmp_path, '[runner]\nmodels = ["claude-opus-5"]\nmodel = "gpt-9"\n')
            )
        assert codes(caught.value) == ["invalid-value"]


class TestTheEnvironmentAllowlist:
    def test_the_credential_is_not_in_scope(self) -> None:
        # The local lane runs a task's `acceptance` on the workstation from a
        # body an agent may have influenced. `gh`'s credential is the one thing
        # that must not be reachable from there.
        runner = RunnerConfig()
        scrubbed = runner.environment(
            {"PATH": "/bin", "GH_TOKEN": "ghp_secret", "GITHUB_TOKEN": "x"}
        )
        assert scrubbed == {"PATH": "/bin"}

    def test_the_ssh_agent_is_not_either(self) -> None:
        # Excluded deliberately: the executor pushes via `gh` in the parent,
        # so the child never needs to authenticate to anything.
        runner = RunnerConfig()
        assert "SSH_AUTH_SOCK" not in runner.environment({"SSH_AUTH_SOCK": "/tmp/agent.sock"})

    def test_anything_that_smells_of_a_secret_is_dropped(self) -> None:
        runner = RunnerConfig()
        hostile = {
            "MY_API_KEY": "1",
            "VENDOR_SECRET": "2",
            "SOME_TOKEN": "3",
            "AWS_SESSION_TOKEN": "4",
            "PASSWORD": "5",
        }
        assert runner.environment(hostile) == {}

    def test_the_floor_survives(self) -> None:
        runner = RunnerConfig()
        floor = {"PATH": "/bin", "HOME": "/u", "LANG": "C", "TERM": "xterm", "TMPDIR": "/tmp"}
        assert runner.environment(floor) == floor

    def test_an_adopter_may_add_a_variable(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, '[runner]\nenv = ["XDG_CONFIG_HOME"]\n'))
        assert config.runner.environment({"XDG_CONFIG_HOME": "/c"}) == {"XDG_CONFIG_HOME": "/c"}

    def test_but_not_by_naming_a_credential(self, tmp_path: Path) -> None:
        # An allowlist that can be told to allow the token is not an
        # allowlist. The graph is agent-authorable; so is a pull request that
        # edits this file.
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, '[runner]\nenv = ["GH_TOKEN"]\n'))
        assert codes(caught.value) == ["refused-env"]

    def test_a_variable_absent_from_the_parent_is_simply_absent(self) -> None:
        assert RunnerConfig().environment({}) == {}


class TestTheDefaults:
    def test_the_shipped_template_names_a_real_cli(self) -> None:
        assert RunnerConfig().argv[0] == "copilot"

    def test_it_passes_the_prompt_as_text_not_as_a_path(self) -> None:
        # `copilot -p` takes the prompt itself; handing it a filename would
        # run whatever that string happened to say.
        assert "{prompt}" in RunnerConfig().argv

    def test_it_renders_without_further_configuration(self) -> None:
        runner = RunnerConfig()
        rendered = runner.render(prompt="do it", model=runner.model)
        assert rendered[0] == "copilot"
        assert "do it" in rendered

    def test_an_unconfigured_repository_still_has_a_runner(self) -> None:
        assert load_config(Path("nope.toml")).runner.argv


class TestTheSchemaStaysClosed:
    def test_an_unknown_runner_key_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(GraphError) as caught:
            load_config(write(tmp_path, '[runner]\nshell = "bash"\n'))
        assert codes(caught.value) == ["unknown-key"]

    def test_runner_is_a_known_top_level_key(self, tmp_path: Path) -> None:
        assert load_config(write(tmp_path, '[runner]\nmodel = "claude-opus-5"\n')).runner.model
