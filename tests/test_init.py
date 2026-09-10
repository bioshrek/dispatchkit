"""D5.5: `dispatchkit init` — make a repository able to run a pass.

`doctor` names what is missing; `init` creates it. The same idempotency
standard as `apply` applies, and for the same reason: this is run against a
repository somebody may already be using, possibly twice, possibly
concurrently with a scheduler pass. So the plan is data, the second plan
against the result must be empty, and nothing that already exists is ever
overwritten.

Since D14 there is no board to set up, and with it went the field-creation
half of this command, the built-in-`Status`-field problem it existed to work
around, and the `--local` split that was really a split at the `project`-scope
boundary. D13 then took the workflow, because there is no unattended pass
left to schedule. What is left creates labels, a config and a directory.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dispatchkit.config import load_config
from dispatchkit.doctor import Diagnostics, LocalFacts, check
from dispatchkit.github import REQUIRED_LABELS
from dispatchkit.init import (
    CONFIG_TEMPLATE,
    NEXT_STEPS,
    CreateLabel,
    MakeDirectory,
    WriteFile,
    execute_init,
    plan_init,
    summarise,
)
from dispatchkit.model import Lane

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeRepository:
    """In-memory label set, so the convergence test can re-read."""

    labels: tuple[str, ...] = ()
    calls: list[str] = field(default_factory=list)

    def fetch_labels(self) -> tuple[str, ...]:
        self.calls.append("fetch_labels()")
        return self.labels

    def ensure_labels(self, labels: Sequence[str]) -> None:
        self.calls.append(f"ensure_labels({sorted(labels)})")
        fresh = tuple(name for name in labels if name not in self.labels)
        self.labels = (*self.labels, *fresh)


def facts(root: Path) -> LocalFacts:
    config = root / ".github" / "dispatchkit.toml"
    plans = root / "docs" / "plans"
    return LocalFacts(
        config_path=config,
        config_exists=config.exists(),
        plans=plans,
        plans_exists=plans.exists(),
    )


class TestPlanning:
    def test_a_bare_repository_gets_every_label(self, tmp_path: Path) -> None:
        plan = plan_init((), facts(tmp_path))
        labels = [op.name for op in plan.operations if isinstance(op, CreateLabel)]
        assert labels == list(REQUIRED_LABELS)

    def test_a_complete_repository_in_a_complete_tree_needs_nothing(self) -> None:
        assert plan_init(REQUIRED_LABELS, facts(ROOT)).operations == ()

    def test_a_bare_tree_gets_a_config_and_a_plans_directory(self, tmp_path: Path) -> None:
        plan = plan_init(REQUIRED_LABELS, facts(tmp_path))
        written = {op.path.name for op in plan.operations if isinstance(op, WriteFile)}
        made = {op.path.name for op in plan.operations if isinstance(op, MakeDirectory)}

        assert written == {"dispatchkit.toml"}
        assert made == {"plans"}

    def test_no_workflow_is_written_any_more(self, tmp_path: Path) -> None:
        # D13: the scheduler runs from the adopter's terminal, so there is
        # nothing to install into `.github/workflows/`. Writing one would be
        # worse than useless — it would run passes nobody asked for, under a
        # token nobody needs to issue.
        plan = plan_init(REQUIRED_LABELS, facts(tmp_path))
        assert not any(
            isinstance(op, WriteFile) and op.path.suffix == ".yml" for op in plan.operations
        )

    def test_an_existing_file_is_never_overwritten(self, tmp_path: Path) -> None:
        # Someone else's config is not ours to rewrite, and this command is
        # explicitly safe to re-run.
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "dispatchkit.toml").write_text("[caps]\ncloud = 9\n")
        plan = plan_init(REQUIRED_LABELS, facts(tmp_path))
        assert not any(
            isinstance(op, WriteFile) and op.path.name == "dispatchkit.toml"
            for op in plan.operations
        )

    def test_nothing_about_a_board_is_planned(self, tmp_path: Path) -> None:
        # D14: the field-creation half is gone, and with it the only reason
        # this command ever needed the `project` scope.
        plan = plan_init((), facts(tmp_path))
        assert all(
            isinstance(op, CreateLabel | WriteFile | MakeDirectory) for op in plan.operations
        )
        assert plan.notices == ()


class TestExecution:
    def test_it_creates_labels_and_files(self, tmp_path: Path) -> None:
        api = FakeRepository()
        result = execute_init(plan_init(api.fetch_labels(), facts(tmp_path)), api)

        assert result.labels == len(REQUIRED_LABELS)
        assert result.files == 1
        assert result.directories == 1
        assert (tmp_path / ".github" / "dispatchkit.toml").exists()
        assert (tmp_path / "docs" / "plans").is_dir()

    def test_labels_are_created_in_one_call_not_one_call_each(self, tmp_path: Path) -> None:
        api = FakeRepository()
        execute_init(plan_init(api.fetch_labels(), facts(tmp_path)), api)
        assert len([call for call in api.calls if call.startswith("ensure_labels")]) == 1

    def test_without_a_credential_the_tree_is_still_written(self, tmp_path: Path) -> None:
        # `--repo` alone decides whether the remote half happens; the local
        # half has never needed anything but a filesystem.
        result = execute_init(plan_init((), facts(tmp_path)), None)
        assert result.labels == 0
        assert (tmp_path / ".github" / "dispatchkit.toml").exists()


class TestIdempotency:
    def test_a_second_pass_over_the_result_plans_nothing(self, tmp_path: Path) -> None:
        # The standard for anything that mutates: apply, re-read, re-plan, and
        # the second plan must be empty.
        api = FakeRepository()
        execute_init(plan_init(api.fetch_labels(), facts(tmp_path)), api)

        again = plan_init(api.fetch_labels(), facts(tmp_path))
        assert again.operations == ()

    def test_running_it_twice_changes_nothing_the_first_run_wrote(self, tmp_path: Path) -> None:
        api = FakeRepository()
        execute_init(plan_init(api.fetch_labels(), facts(tmp_path)), api)
        written = (tmp_path / ".github" / "dispatchkit.toml").read_text(encoding="utf-8")

        execute_init(plan_init(api.fetch_labels(), facts(tmp_path)), api)
        assert (tmp_path / ".github" / "dispatchkit.toml").read_text(encoding="utf-8") == written


class TestTheResultIsHealthy:
    """The two commands are one contract, so they are tested as one.

    `init` creating something `doctor` still complains about — or `doctor`
    demanding something `init` never creates — is the failure mode that makes
    an on-ramp worse than no on-ramp.

    There is exactly one permitted exception, and it is a real one: branch
    protection. `init` cannot know which status check this repository's
    `acceptance` command runs, so it cannot add the rule; what it can do is
    finish by saying so, which `TestNextSteps` covers.
    """

    def _remaining(self, tmp_path: Path) -> list[str]:
        api = FakeRepository()
        execute_init(plan_init(api.fetch_labels(), facts(tmp_path)), api)
        checks = check(
            Diagnostics(scopes=("repo",), agent_available=True, labels=api.fetch_labels()),
            facts(tmp_path),
        )
        return [item.name for item in checks if not item.ok]

    def test_only_the_human_supplied_input_is_left_outstanding(self, tmp_path: Path) -> None:
        assert self._remaining(tmp_path) == ["merge-gate"]


class TestTemplatesMatchThisRepository:
    """What `init` writes is held to this repository's own standards.

    Only the config is left to pin. It is pinned byte-for-byte because it has
    no asymmetry between here and an adopter's tree — the workflow did, which
    is why it was never pinned, and D13 has now deleted it outright.
    """

    def test_the_config_template_is_this_repositorys_config(self) -> None:
        on_disk = (ROOT / ".github" / "dispatchkit.toml").read_text(encoding="utf-8")
        assert CONFIG_TEMPLATE == on_disk

    def test_the_config_template_states_the_defaults_it_documents(self, tmp_path: Path) -> None:
        # A template that does not parse, or that quietly changes a cap, would
        # be found by an adopter rather than by us.
        path = tmp_path / "dispatchkit.toml"
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        config = load_config(path)

        assert (config.cap(Lane.CLOUD), config.cap(Lane.LOCAL)) == (3, 1)
        assert config.retry_budget == 3
        assert config.plans == Path("docs/plans")
        assert config.is_fenced(".github/workflows/dispatchkit.yml")

    def test_the_template_shows_what_will_run_on_the_adopters_machine(
        self, tmp_path: Path
    ) -> None:
        # The runner argv is the one default that executes something, so it is
        # written out rather than left implicit: an adopter should not have to
        # read our source to find out what `lane = "local"` starts.
        path = tmp_path / "dispatchkit.toml"
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        runner = load_config(path).runner

        assert runner.render(prompt="do it", model=runner.model)[0] == "copilot"
        assert runner.allows("claude-sonnet-5")
        assert "GH_TOKEN" not in runner.environment({"GH_TOKEN": "x", "PATH": "/bin"})


class TestNextSteps:
    """`init` must not leave the adopter believing the setup is complete.

    Two things stay manual. Branch protection, because `init` cannot know
    which status check an `acceptance` command runs; and the Copilot approval
    setting, which is not exposed over REST at all. Since `init` cannot supply
    either, the least it can do is end by naming them.
    """

    def _lines(self, tmp_path: Path) -> list[str]:
        return summarise(plan_init(REQUIRED_LABELS, facts(tmp_path)))

    def test_it_names_the_branch_protection_it_cannot_add(self, tmp_path: Path) -> None:
        printed = "\n".join(self._lines(tmp_path))
        assert "protection" in printed

    def test_it_no_longer_asks_for_a_board(self, tmp_path: Path) -> None:
        printed = "\n".join(self._lines(tmp_path))
        assert "DISPATCHKIT_PROJECT" not in printed

    def test_it_no_longer_asks_for_a_token_or_a_plan_variable(self, tmp_path: Path) -> None:
        # D13: the scheduler runs as the adopter, from their own terminal, on
        # the credential `gh` already holds. Asking them to mint a long-lived
        # PAT and install it as a repository secret would be asking for a
        # standing grant that nothing now reads.
        printed = "\n".join(self._lines(tmp_path))
        assert "DISPATCHKIT_TOKEN" not in printed
        assert "DISPATCHKIT_PLAN" not in printed

    def test_it_names_the_copilot_approval_setting(self, tmp_path: Path) -> None:
        # The fourth manual step, and the only one that is not a command:
        # until it is turned off, every agent-authored CI run waits for a
        # human click, so `verify: auto` cannot complete unattended. It is
        # not exposed over REST, so neither `init` nor `doctor` can act on
        # it and naming it is the whole of what we can do.
        printed = "\n".join(self._lines(tmp_path))
        assert "Actions workflow approval" in printed
        assert "verify: auto" in printed

    def test_the_approval_setting_is_not_offered_as_a_command(self, tmp_path: Path) -> None:
        # It is UI-only. Printing it under NEXT, among lines that can be
        # pasted into a shell, would invite an adopter to try.
        for line in self._lines(tmp_path):
            if "Actions workflow approval" in line:
                assert line.startswith("MANUAL ")

    def test_the_cost_of_turning_it_off_is_stated(self, tmp_path: Path) -> None:
        # It is a trust decision, not a formality: it lets unreviewed agent
        # code run workflows. Naming the step without its cost would be
        # advice we cannot stand behind.
        printed = "\n".join(self._lines(tmp_path))
        assert "unreviewed" in printed

    def test_the_steps_are_shown_even_when_there_is_nothing_to_do(self, tmp_path: Path) -> None:
        # A second `init` is a no-op on the repository, but the manual steps are
        # exactly what an adopter re-runs it to be reminded of.
        plan = plan_init(REQUIRED_LABELS, facts(tmp_path))
        assert [line for line in summarise(plan) if line.startswith("NEXT")]

    def test_every_step_is_a_command_that_can_be_run(self, tmp_path: Path) -> None:
        for step in NEXT_STEPS:
            assert step.startswith("gh ")
