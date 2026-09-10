"""D16: the cloud lane is told which branch to start from.

This is the fact the whole deliverable rested on. The coding agent opens its
own pull request, so nothing dispatchkit writes into a body controls where that
work is based — which looked like a reason the cloud lane could not honour a
plan branch at all, and a lane asymmetry would have been worse than the problem
it solved.

It can. `replaceActorsForAssignable`, the mutation the assignment already uses,
takes an `agentAssignment` input carrying `baseRef`: *"The base ref/branch for
the repository. Defaults to the default branch if not provided."* Confirmed by
introspecting the live schema, not read from documentation.

So the base is sent with the assignment, and the two lanes stay symmetric. It
is sent as a GraphQL *variable*, never spliced into the mutation text, for the
same reason the two ids already are.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.gh_cli import assign_command
from dispatchkit.github import AssignAgent
from dispatchkit.model import DEFAULT_BASE, Base, Lane, Verify
from dispatchkit.tick import plan_tick
from tests.items import issue, state_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class TestTheCommand:
    def test_the_base_travels_as_a_variable(self) -> None:
        command = assign_command("I_1", "A_1", base=Base("plan/wordfreq"))
        assert "baseRef=plan/wordfreq" in command

    def test_it_is_never_spliced_into_the_query(self) -> None:
        command = assign_command("I_1", "A_1", base=Base("plan/wordfreq"))
        query = next(arg for arg in command if arg.startswith("query="))
        assert "plan/wordfreq" not in query

    def test_the_mutation_declares_the_input(self) -> None:
        command = assign_command("I_1", "A_1", base=DEFAULT_BASE)
        query = next(arg for arg in command if arg.startswith("query="))
        assert "agentAssignment" in query and "baseRef" in query

    def test_every_argument_is_its_own_word(self) -> None:
        command = assign_command("I_1", "A_1", base=Base("plan/wordfreq"))
        assert all(isinstance(arg, str) for arg in command)
        assert command[0] == "gh"


class TestTheDispatchCarriesIt:
    def one_task(self, base: Base) -> AssignAgent:
        state = state_of(issue("one", number=1, lane=Lane.CLOUD, verify=Verify.HUMAN, base=base))
        plan = plan_tick(state, config=SchedulerConfig(), now=NOW)
        return next(op for op in plan.operations if isinstance(op, AssignAgent))

    def test_the_plans_base_reaches_the_operation(self) -> None:
        assert self.one_task(Base("plan/wordfreq")).base == Base("plan/wordfreq")

    def test_a_plan_without_one_still_says_main(self) -> None:
        # Explicit rather than absent: the adapter should never have to decide
        # what "no base" means.
        assert self.one_task(DEFAULT_BASE).base == DEFAULT_BASE
