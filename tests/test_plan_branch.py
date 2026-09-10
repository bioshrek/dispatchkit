"""D16: `apply` makes the branch the plan integrates on.

A plan declares its base, and the base has to exist before anything can be cut
from it: the local lane fetches it, and the cloud agent is handed it as a
`baseRef`. Nobody was going to create it — `apply` is the only command that
runs when a plan is written, so it is the only one that can.

It is done the way labels are done, not the way issues are done, and the
difference is the point. Issues are diffed: the plan says what to create
because the state says what exists. A branch is *ensured* — asked for
unconditionally, and the adapter absorbs "already there". That keeps it out of
the operation list, which is what the convergence test reads, so a second
`apply` still plans nothing while still guaranteeing the branch. Diffing it
would have meant teaching `RepoState` about refs to answer a question the API
already answers idempotently.

A plan on `main` asks for nothing. `main` exists, and creating the default
branch is not a thing that can succeed.
"""

from __future__ import annotations

import pytest

from dispatchkit.apply import execute_plan, plan_apply
from dispatchkit.github import RepoState
from dispatchkit.model import DEFAULT_BASE, Base
from tests.fake_github import FakeGitHub
from tests.graphs import graph, task

pytestmark = pytest.mark.unit

PLAN_BASE = Base("plan/wordfreq")


def apply_with(base: Base) -> FakeGitHub:
    api = FakeGitHub(state=RepoState(()))
    plan = graph(task("a"), base=base)
    execute_plan(plan_apply(plan, api.fetch_state()), api=api)
    return api


class TestTheBranchIsMade:
    def test_a_plan_branch_is_ensured(self) -> None:
        assert apply_with(PLAN_BASE).ensured_branches == [str(PLAN_BASE)]

    def test_a_plan_on_main_asks_for_nothing(self) -> None:
        assert apply_with(DEFAULT_BASE).ensured_branches == []


class TestItIsEnsuredRatherThanPlanned:
    def test_it_is_not_an_operation(self) -> None:
        # The operation list is what `apply` prints and what the convergence
        # test reads. A branch that is always ensured would otherwise make
        # every plan non-empty for ever.
        api = FakeGitHub(state=RepoState(()))
        plan = plan_apply(graph(task("a"), base=PLAN_BASE), api.fetch_state())
        execute_plan(plan, api=api)
        again = plan_apply(graph(task("a"), base=PLAN_BASE), api.fetch_state())
        assert not again.operations

    def test_ensuring_twice_is_harmless(self) -> None:
        api = FakeGitHub(state=RepoState(()))
        plan = graph(task("a"), base=PLAN_BASE)
        execute_plan(plan_apply(plan, api.fetch_state()), api=api)
        execute_plan(plan_apply(plan, api.fetch_state()), api=api)
        assert api.ensured_branches == [str(PLAN_BASE), str(PLAN_BASE)]


class TestTheAdapter:
    """Three `gh api` calls, and the order is what makes it safe.

    Ask for the ref first: if it is there, nothing else runs. Only then is the
    default branch resolved and a ref created from its tip. Creating first and
    absorbing the 422 would be shorter and would also mean a plan branch that
    someone had moved forward got reasoned about by an error message.
    """

    def test_the_existence_check_is_a_read(self) -> None:
        from dispatchkit.gh_cli import branch_ref_command

        assert branch_ref_command("o/r", PLAN_BASE) == [
            "gh",
            "api",
            "repos/o/r/git/ref/heads/plan/wordfreq",
        ]

    def test_the_default_branch_is_asked_for_rather_than_assumed(self) -> None:
        from dispatchkit.gh_cli import default_branch_command

        # Not "main": the repository's default branch is a repository setting,
        # and DEFAULT_BASE is dispatchkit's word for "this plan has no branch
        # of its own". Conflating them would cut a plan branch from a branch
        # that need not exist.
        assert default_branch_command("o/r") == [
            "gh",
            "api",
            "repos/o/r",
            "--jq",
            ".default_branch",
        ]

    def test_creating_the_ref_is_a_post_with_the_tip_sha(self) -> None:
        from dispatchkit.gh_cli import create_branch_command

        assert create_branch_command("o/r", PLAN_BASE, "abc123") == [
            "gh",
            "api",
            "repos/o/r/git/refs",
            "--method",
            "POST",
            "-f",
            "ref=refs/heads/plan/wordfreq",
            "-f",
            "sha=abc123",
        ]

    def test_every_part_is_an_argv_entry(self) -> None:
        from dispatchkit.gh_cli import create_branch_command

        # The base reaches a command line, which is why it is a value object
        # with a leading `-` refused: an option is not a ref.
        assert all(isinstance(part, str) for part in create_branch_command("o/r", PLAN_BASE, "a"))
