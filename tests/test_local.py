"""D6.3: one local task, end to end.

The executor owns everything except the change itself. It fetches, branches,
makes a worktree, builds the prompt, invokes the agent, runs `acceptance`,
pushes and opens the pull request — and the agent is handed a prepared,
disposable tree and asked to do exactly one thing.

The ordering is not arbitrary and most of these tests are about it:

- `acceptance` runs **before** the pull request, because a PR is a claim that
  the work is done and `verify: auto` would merge it;
- the push happens in the **parent**, because the child has no credential and
  no agent socket — the separation falls out of the environment allowlist
  rather than having to be remembered;
- `Closes #N` is written by the executor and never left to the prompt, since an
  agent that forgot it would merge a pull request while leaving the issue open,
  stalling every dependent with no error anywhere;
- a failed run **keeps** its worktree and pushes its branch, because evidence
  is the only thing a failed run produces, and it is evidence rather than
  state: nothing reads it back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.local import run_local
from dispatchkit.resolve import TaskItem
from dispatchkit.workstation import RunResult, acceptance_argv, prompt_for, strip_block
from tests.fake_github import FakeGitHub
from tests.fake_workstation import ROOT, FakeWorkstation
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

CONFIG = SchedulerConfig()
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def task_of(api: FakeGitHub, name: str = "gpu") -> TaskItem:
    from dispatchkit.resolve import build_items

    items, _ = build_items(api.fetch_state())
    return next(item for item in items if item.id == name)


def setup(**issue_kwargs: object) -> tuple[FakeGitHub, FakeWorkstation]:
    from dispatchkit.model import Lane

    defaults: dict[str, object] = {"lane": Lane.LOCAL}
    defaults.update(issue_kwargs)
    api = FakeGitHub(state=state_of(issue("gpu", 1, **defaults)))  # type: ignore[arg-type]
    return api, FakeWorkstation()


class TestTheHappyPath:
    def test_it_reports_success(self) -> None:
        api, machine = setup()
        run = run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert run.ok

    def test_the_worktree_is_made_before_the_agent_runs(self) -> None:
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert machine.calls.index("create_worktree(/work/demo/gpu, dispatchkit/demo/gpu/1)") < (
            machine.calls.index("run(copilot)")
        )

    def test_the_agent_runs_in_the_worktree(self) -> None:
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        _, cwd, _ = machine.runs[0]
        assert cwd == Path("/work/demo/gpu")

    def test_acceptance_runs_before_the_pull_request(self) -> None:
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        commands = [call for call in machine.calls if call.startswith("run(")]
        assert commands[-1] != "run(copilot)"

    def test_the_pull_request_closes_the_issue(self) -> None:
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert "Closes #1" in api.opened[0]["body"]

    def test_the_branch_is_pushed_from_the_parent(self) -> None:
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert machine.pushed == ["dispatchkit/demo/gpu/1"]

    def test_the_worktree_is_removed_on_success(self) -> None:
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert machine.removed == [Path("/work/demo/gpu")]

    def test_the_mark_comes_off_so_the_next_pass_sees_the_truth(self) -> None:
        # The label is the local lane's assignment. Leaving it on would make
        # the task read `Dispatched` for ever -- the D6.0 bug, from the other
        # end.
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert any("dispatch:local" in call for call in api.calls if "edit_labels" in call)

    def test_the_log_tail_is_posted_to_the_issue(self) -> None:
        # The trace lives on GitHub rather than only on one workstation.
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert api.comments


class TestTheAgentIsToldOnlyWhatItCanAct_On:
    def test_the_prompt_carries_the_prose(self) -> None:
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        argv, _, _ = machine.runs[0]
        assert "gpu" in argv[argv.index("-p") + 1]

    def test_the_machine_block_is_not_in_it(self) -> None:
        # `touches` would read as a permission boundary. It is an advisory
        # scheduling hint, and handing it over invites an agent to treat a
        # guess as a constraint.
        api, machine = setup(touches=("src/only/**",))
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        argv, _, _ = machine.runs[0]
        prompt = argv[argv.index("-p") + 1]
        assert "dispatchkit" not in prompt
        assert "src/only/**" not in prompt

    def test_the_child_never_sees_the_credential(self) -> None:
        api, machine = setup()
        run_local(
            task_of(api),
            api=api,
            machine=machine,
            config=CONFIG,
            root=ROOT,
            now=NOW,
            environ={"PATH": "/bin", "GH_TOKEN": "ghp_secret"},
        )
        for _, _, env in machine.runs:
            assert "GH_TOKEN" not in env
            assert env["PATH"] == "/bin"


class TestWhenTheAgentFails:
    def test_no_pull_request_is_opened(self) -> None:
        api, machine = setup()
        machine.outcomes["copilot"] = RunResult(("copilot",), 1, "gave up")
        run = run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert not run.ok
        assert api.opened == []

    def test_acceptance_is_not_run_either(self) -> None:
        # There is nothing to accept. Running it would report a second failure
        # for the same cause and bury the first.
        api, machine = setup()
        machine.outcomes["copilot"] = RunResult(("copilot",), 1, "gave up")
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert [call for call in machine.calls if call.startswith("run(")] == ["run(copilot)"]

    def test_the_worktree_is_kept_for_inspection(self) -> None:
        api, machine = setup()
        machine.outcomes["copilot"] = RunResult(("copilot",), 1, "gave up")
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert machine.removed == []

    def test_the_mark_still_comes_off(self) -> None:
        # Releasing rather than resuming is what keeps the retry budget
        # honest: the next pass re-marks it, and the mark is the event
        # `attempts` derives from.
        api, machine = setup()
        machine.outcomes["copilot"] = RunResult(("copilot",), 1, "gave up")
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert any("-['dispatch:local']" in call for call in api.calls)

    def test_the_failure_is_reported_on_the_issue(self) -> None:
        api, machine = setup()
        machine.outcomes["copilot"] = RunResult(("copilot",), 1, "the model gave up")
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert "the model gave up" in api.comments[0]["body"]

    def test_a_timeout_says_it_timed_out(self) -> None:
        # The one case recovery cannot see -- worktree present, agent wedged --
        # so the supervisor that owns the child says so itself.
        api, machine = setup()
        machine.outcomes["copilot"] = RunResult(("copilot",), -9, "", timed_out=True)
        run = run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert "timed out" in api.comments[0]["body"]
        assert not run.ok


class TestWhenAcceptanceFails:
    def test_no_pull_request_is_opened(self) -> None:
        api, machine = setup()
        machine.outcomes["check"] = RunResult(("check",), 1, "1 failed")
        run = run_local(
            task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW
        )
        assert not run.ok
        assert api.opened == []

    def test_the_branch_is_still_pushed_as_evidence(self) -> None:
        # Evidence, not state: a human may want to read it and nothing in the
        # system does.
        api, machine = setup()
        machine.commit_counts[Path("/work/demo/gpu")] = 3
        machine.outcomes["check"] = RunResult(("check",), 1, "1 failed")
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert machine.pushed == ["dispatchkit/demo/gpu/1"]

    def test_a_run_with_no_commits_pushes_nothing(self) -> None:
        api, machine = setup()
        machine.outcomes["check"] = RunResult(("check",), 1, "1 failed")
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert machine.pushed == []

    def test_every_clause_is_run_until_one_fails(self) -> None:
        api, machine = setup()
        machine.outcomes["check"] = RunResult(("check",), 1, "no")
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        commands = [call for call in machine.calls if call.startswith("run(")]
        assert commands == ["run(copilot)", "run(check)"]


class TestTheAttemptNumber:
    def test_the_first_run_is_attempt_one(self) -> None:
        api, machine = setup()
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert machine.pushed == ["dispatchkit/demo/gpu/1"]

    def test_a_retry_gets_its_own_branch(self) -> None:
        # Or it would force-push over the evidence the first run left.
        api, machine = setup(holds=(), dispatches=(NOW,))
        run_local(task_of(api), api=api, machine=machine, config=CONFIG, root=ROOT, now=NOW)
        assert machine.pushed == ["dispatchkit/demo/gpu/2"]


class TestThePureParts:
    def test_the_block_comes_out_of_the_body(self) -> None:
        body = "do the thing\n\n<!-- dispatchkit\nid: a\n-->\n"
        assert strip_block(body) == "do the thing"

    def test_a_body_with_no_block_is_untouched(self) -> None:
        assert strip_block("just prose") == "just prose"

    def test_an_unterminated_block_takes_everything_after_it(self) -> None:
        # Fail closed: half a block is still the scheduler's vocabulary.
        assert strip_block("prose\n<!-- dispatchkit\nid: a") == "prose"

    def test_the_prompt_leads_with_the_title(self) -> None:
        assert prompt_for("Add flags", "some prose").startswith("Add flags")

    def test_acceptance_splits_on_and_and(self) -> None:
        assert acceptance_argv("uv run pytest && uv run lint") == (
            ("uv", "run", "pytest"),
            ("uv", "run", "lint"),
        )

    def test_quotes_are_respected_but_never_expanded(self) -> None:
        assert acceptance_argv("grep 'two words' file") == (("grep", "two words", "file"),)

    def test_a_shell_metacharacter_is_just_a_word(self) -> None:
        # `shlex` finds the words a shell would have found without letting a
        # shell find them: no expansion, no substitution, no chaining.
        assert acceptance_argv("echo '$(whoami)'") == (("echo", "$(whoami)"),)

    def test_an_empty_clause_is_dropped(self) -> None:
        assert acceptance_argv("a && && b") == (("a",), ("b",))


class TestALocalAttemptCounts:
    """The retry budget has to see a local dispatch, or it never runs out.

    `attempts` derives from ASSIGNED_EVENTs, because the cloud lane dispatches
    by assigning. A local task is never assigned to anybody -- it is marked --
    so without this the count stays at zero, `stuck` is never reached, and a
    task that can never pass is retried for ever. The mark is the event.
    """

    def test_the_mark_is_counted(self) -> None:
        from dispatchkit.gh_cli import _parse_dispatches

        node = {
            "dispatches": {"nodes": []},
            "holds": {
                "nodes": [
                    {"createdAt": "2026-09-10T09:00:00Z", "label": {"name": "dispatch:local"}},
                ]
            },
        }
        assert _parse_dispatches(node) == (datetime(2026, 9, 10, 9, 0, tzinfo=UTC),)

    def test_other_labels_are_not_dispatches(self) -> None:
        from dispatchkit.gh_cli import _parse_dispatches

        node = {
            "dispatches": {"nodes": []},
            "holds": {
                "nodes": [
                    {"createdAt": "2026-09-10T09:00:00Z", "label": {"name": "dispatch:hold"}},
                    {"createdAt": "2026-09-10T09:30:00Z", "label": {"name": "dispatchkit"}},
                ]
            },
        }
        assert _parse_dispatches(node) == ()

    def test_both_lanes_land_in_one_ordered_sequence(self) -> None:
        # A task that was tried in one lane and re-planned into the other still
        # has one budget, and the runs must stay in order for the hold discount
        # to pair each dispatch with the one that superseded it.
        from dispatchkit.gh_cli import _parse_dispatches

        node = {
            "dispatches": {
                "nodes": [
                    {
                        "createdAt": "2026-09-10T10:00:00Z",
                        "assignee": {"login": "copilot-swe-agent"},
                    }
                ]
            },
            "holds": {
                "nodes": [
                    {"createdAt": "2026-09-10T09:00:00Z", "label": {"name": "dispatch:local"}},
                ]
            },
        }
        assert _parse_dispatches(node) == (
            datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
            datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
        )
