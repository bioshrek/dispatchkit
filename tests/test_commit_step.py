"""D6.6: somebody has to commit the agent's work, and nobody did.

The fifth live run got all the way to the end and died there:

    GraphQL: No commits between main and dispatchkit/wordfreq/version-flag/3

The agent had done the work — three files modified in the worktree, exactly the
three the task declared — and left them uncommitted. `run_local` goes agent,
acceptance, push, open PR, and there is no commit anywhere in it. Nothing in
the prompt asks for one either: `prompt_for` is the issue title and body with
the machine block stripped.

So `git push` pushed a branch identical to `main`, and GitHub refused the pull
request. The traceback was the visible half. The dangerous half is what happens
when it *doesn't* fail: acceptance had already passed, on the uncommitted
working tree, so the pipeline verified work it was about to discard. An empty
branch that opened successfully would carry a green CI run — it is `main` — and
`verify: auto` would merge nothing at all while reporting the task done.

Committing belongs to the executor, not the agent. The executor already owns
git here: it makes the worktree, and it pushes, because the child runs with no
credential. Asking the agent to commit would make the pipeline's correctness
depend on a sentence in a prompt.

The commit goes in *before* acceptance rather than after. Committing does not
change the working tree, so acceptance sees exactly what the agent produced
either way — but a run that fails acceptance then still has a real commit on
the branch it keeps, which is what makes "kept for inspection" true.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.local import LocalRun, run_local
from dispatchkit.model import Lane, TaskId
from dispatchkit.resolve import TaskItem, build_items
from dispatchkit.workstation import RunResult
from tests.fake_github import FakeGitHub
from tests.fake_workstation import FakeWorkstation
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
ROOT = Path("/tmp/dispatchkit-test")


def task_of(api: FakeGitHub) -> TaskItem:
    resolved, _ = build_items(api.fetch_state())
    return next(item for item in resolved if item.ref.id == TaskId("version-flag"))


def go(machine: FakeWorkstation, api: FakeGitHub | None = None) -> LocalRun:
    api = api or FakeGitHub(state=state_of(issue("version-flag", number=1, lane=Lane.LOCAL)))
    return run_local(
        task_of(api),
        api=api,
        machine=machine,
        config=SchedulerConfig(),
        root=ROOT,
        now=NOW,
    )


class TestTheWorkIsCommitted:
    def test_a_commit_is_made(self) -> None:
        machine = FakeWorkstation(commit_counts={ROOT / "demo" / "version-flag": 1})
        go(machine)

        assert machine.committed, machine.calls

    def test_it_happens_before_acceptance(self) -> None:
        # So a run that fails acceptance still has something on the branch it
        # keeps. Committing does not touch the working tree, so acceptance
        # sees the same files either way.
        machine = FakeWorkstation(commit_counts={ROOT / "demo" / "version-flag": 1})
        go(machine)

        order = [call for call in machine.calls if call.startswith(("commit(", "run("))]
        # The first `run(` is the agent itself; acceptance is everything after.
        assert order[1].startswith("commit("), order

    def test_it_happens_before_the_push(self) -> None:
        machine = FakeWorkstation(commit_counts={ROOT / "demo" / "version-flag": 1})
        go(machine)

        names = [call.split("(")[0] for call in machine.calls]
        assert names.index("commit") < names.index("push")

    def test_the_pull_request_is_still_opened(self) -> None:
        api = FakeGitHub(state=state_of(issue("version-flag", number=1, lane=Lane.LOCAL)))
        machine = FakeWorkstation(commit_counts={ROOT / "demo" / "version-flag": 1})

        run = go(machine, api)

        assert run.ok, run.detail
        assert run.pr is not None


class TestAnAgentThatChangedNothing:
    """The case that produced the traceback, reported instead."""

    def test_it_fails_rather_than_opening_a_pull_request(self) -> None:
        # No commits ahead of base: `commit_counts` defaults to zero.
        machine = FakeWorkstation()

        run = go(machine)

        assert not run.ok

    def test_no_pull_request_is_attempted(self) -> None:
        api = FakeGitHub(state=state_of(issue("version-flag", number=1, lane=Lane.LOCAL)))
        machine = FakeWorkstation()

        go(machine, api)

        assert not any(call.startswith("open_pr") for call in api.calls), api.calls

    def test_nothing_is_pushed(self) -> None:
        machine = FakeWorkstation()

        go(machine)

        assert machine.pushed == []

    def test_it_says_so_in_words(self) -> None:
        run = go(FakeWorkstation())

        assert "no" in run.detail.lower() and "change" in run.detail.lower(), run.detail


class TestACommitThatFails:
    def test_a_failure_is_reported_not_raised(self) -> None:
        machine = FakeWorkstation(
            commit_result=RunResult(("git", "commit"), 1, "nothing to commit")
        )

        run = go(machine)

        assert not run.ok
        assert run.stage == "commit"
