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
boundary. What is left creates labels, a config, a workflow and a directory.
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
    workflow = root / ".github" / "workflows" / "dispatchkit.yml"
    plans = root / "docs" / "plans"
    return LocalFacts(
        config_path=config,
        config_exists=config.exists(),
        workflow_path=workflow,
        workflow_exists=workflow.exists(),
        plans=plans,
        plans_exists=plans.exists(),
        workflow_text=workflow.read_text(encoding="utf-8") if workflow.exists() else "",
        vendored=(root / "src" / "dispatchkit").is_dir(),
    )


class TestPlanning:
    def test_a_bare_repository_gets_every_label(self, tmp_path: Path) -> None:
        plan = plan_init((), facts(tmp_path))
        labels = [op.name for op in plan.operations if isinstance(op, CreateLabel)]
        assert labels == list(REQUIRED_LABELS)

    def test_a_complete_repository_in_a_complete_tree_needs_nothing(self) -> None:
        assert plan_init(REQUIRED_LABELS, facts(ROOT)).operations == ()

    def test_a_bare_tree_gets_a_config_a_workflow_and_a_plans_directory(
        self, tmp_path: Path
    ) -> None:
        plan = plan_init(REQUIRED_LABELS, facts(tmp_path))
        written = {op.path.name for op in plan.operations if isinstance(op, WriteFile)}
        made = {op.path.name for op in plan.operations if isinstance(op, MakeDirectory)}

        assert written == {"dispatchkit.toml", "dispatchkit.yml"}
        assert made == {"plans"}

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
        assert result.files == 2
        assert result.directories == 1
        assert (tmp_path / ".github" / "workflows" / "dispatchkit.yml").exists()
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
        assert (tmp_path / ".github" / "workflows" / "dispatchkit.yml").exists()


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

    There is exactly one permitted exception, and it is a real one: the token
    and the repository variable naming the plan. `init` holds no credential to
    install and must not invent a plan name, so it cannot supply them; what it
    can do is finish by saying so, which `TestNextSteps` covers.
    """

    def _remaining(self, tmp_path: Path) -> list[str]:
        api = FakeRepository()
        execute_init(plan_init(api.fetch_labels(), facts(tmp_path)), api)
        checks = check(
            Diagnostics(scopes=("repo",), agent_available=True, labels=api.fetch_labels()),
            facts(tmp_path),
        )
        return [item.name for item in checks if not item.ok]

    def test_only_the_human_supplied_inputs_are_left_outstanding(self, tmp_path: Path) -> None:
        # Branch protection joins the list for the same reason: `init` cannot
        # know which status check this repository's `acceptance` runs.
        assert self._remaining(tmp_path) == ["workflow-inputs", "merge-gate"]

    def test_the_workflow_it_writes_can_import_dispatchkit(self, tmp_path: Path) -> None:
        # The regression that matters: `init` used to write a workflow that
        # only worked in dispatchkit's own repository.
        assert "workflow-source" not in self._remaining(tmp_path)


class TestTemplatesMatchThisRepository:
    """What `init` writes is held to this repository's own standards.

    The workflow is deliberately *not* pinned byte-for-byte to the one here.
    It used to be, and the pin was wrong: this repository has `src/dispatchkit`
    in its tree and an adopter does not, so a template identical to ours could
    not run anywhere but here — which is exactly what happened. The guarantee
    now lives in `tests/test_workflow.py`, which asserts every permission,
    trigger and script-safety property against *both* files, plus the ones that
    can only be true of one of them.

    The config template is a different matter: it has no such asymmetry, so it
    stays pinned.
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


class TestNextSteps:
    """`init` must not leave the adopter believing the setup is complete.

    Two things stay manual — a token, and the variable naming the plan.
    Without them the scheduler runs on its cron and exits with empty
    arguments, which is a failure nobody sees. Since `init` cannot supply
    them, the least it can do is end by naming them.
    """

    def _lines(self, tmp_path: Path) -> list[str]:
        return summarise(plan_init(REQUIRED_LABELS, facts(tmp_path)))

    def test_it_names_the_token_and_the_plan_variable(self, tmp_path: Path) -> None:
        printed = "\n".join(self._lines(tmp_path))
        assert "DISPATCHKIT_TOKEN" in printed
        assert "DISPATCHKIT_PLAN" in printed

    def test_it_no_longer_asks_for_a_board(self, tmp_path: Path) -> None:
        printed = "\n".join(self._lines(tmp_path))
        assert "DISPATCHKIT_PROJECT" not in printed

    def test_the_token_no_longer_has_to_be_a_classic_pat(self, tmp_path: Path) -> None:
        # It had to be one only because a fine-grained token silently cannot
        # touch a user-owned Project. With the board gone, `repo` is the whole
        # requirement and any token shape that grants it will do.
        token = next(step for step in NEXT_STEPS if "DISPATCHKIT_TOKEN" in step)
        assert "repo" in token
        assert "classic" not in token

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
