"""D5.6: what CI is currently saying about a pull request.

`verify: auto` promises that CI, not a human, decides whether a task's PR may
merge. That promise is only keepable if the scheduler can tell the difference
between *CI said yes*, *CI said no*, and *CI never got to speak* — the last of
which is the normal state of a coding agent's PR, because GitHub holds
workflow runs on agent-authored branches until a human approves them.

The precedence rule is the whole of the domain logic, and it is ordered by who
has to act: a suite nobody can start outranks a suite that failed, which
outranks one still running.
"""

from __future__ import annotations

import pytest

from dispatchkit.model import Checks, PullRequest

pytestmark = pytest.mark.unit


class TestCombine:
    def test_no_suites_at_all_is_none(self) -> None:
        assert Checks.combine(()) is Checks.NONE

    def test_all_green_is_passing(self) -> None:
        assert Checks.combine((Checks.PASSING, Checks.PASSING)) is Checks.PASSING

    def test_one_still_running_holds_the_verdict_open(self) -> None:
        assert Checks.combine((Checks.PASSING, Checks.PENDING)) is Checks.PENDING

    def test_a_failure_outranks_work_still_in_progress(self) -> None:
        assert Checks.combine((Checks.PENDING, Checks.FAILING)) is Checks.FAILING

    def test_a_run_awaiting_approval_outranks_everything(self) -> None:
        # Nothing else matters if a human has to press a button first: the
        # other suites' verdicts are about a pipeline that is not complete.
        parts = (Checks.PASSING, Checks.FAILING, Checks.BLOCKED)
        assert Checks.combine(parts) is Checks.BLOCKED


class TestStalled:
    @pytest.mark.parametrize("checks", [Checks.BLOCKED, Checks.FAILING])
    def test_ci_cannot_reach_a_verdict_alone(self, checks: Checks) -> None:
        assert checks.stalled

    @pytest.mark.parametrize("checks", [Checks.NONE, Checks.PENDING, Checks.PASSING])
    def test_ci_is_still_carrying_the_pull_request(self, checks: Checks) -> None:
        # `NONE` counts as carrying on purpose. With no stored state we cannot
        # tell "the runs have not been created yet" from "this repository has
        # no CI", and the first is the common case in the seconds after a PR
        # opens. Claiming a stall on absence would cry wolf on every new PR;
        # `BLOCKED` is an explicit statement from GitHub, so that is the one
        # the scheduler acts on.
        assert not checks.stalled


class TestPullRequest:
    def test_carries_its_number_and_verdict(self) -> None:
        pr = PullRequest(number=7, checks=Checks.BLOCKED)
        assert (pr.number, pr.checks) == (7, Checks.BLOCKED)

    def test_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            PullRequest(number=7, checks=Checks.NONE).number = 8  # type: ignore[misc]
