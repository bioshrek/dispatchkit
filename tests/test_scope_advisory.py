"""D9.2: `touches` advises the scheduler; it does not hold merge authority.

A walk-back, and the reason is recorded in the design rather than discovered
again. §"Parallel work and file conflicts" settled this at the start:

    Non-overlapping file scope is **not** a hard requirement, and trying to
    make it one would be a mistake. File sets can't be known accurately before
    an agent starts work, so a declared scope is either over-broad
    (serialising everything and destroying the parallelism we're buying) or
    under-broad (giving false confidence).

The four conflict layers follow from that: decomposition, an *advisory*
exclusion, the merge queue -- "the layer that actually guarantees correctness,
and it is why layer 2 can stay advisory" -- and a scope-drift check. Layer 4
then promoted layer 2's guess into merge authority, and the promotion is the
defect. A hint whose stated worst case was "extra serialisation, never a bad
merge" acquired a worse one: refusing a good merge, for ever, because a
prediction made before the work was narrower than the work.

That fired live. `document-flags` declared `touches = ["README.md"]` while its
own brief asked the agent, in prose, to add `tests/test_readme.py`. The agent
did as it was told and the pipeline refused it. The plan stated the scope
twice and disagreed with itself; the gate sided with the copy the agent was
never shown, since §"the prompt" withholds `touches` from the agent precisely
because it "is an advisory exclusion hint, not a permission boundary".

So the authority moves to the boundary that can carry it. The blast-radius
fence is repo-level, declared in config rather than predicted per task, cannot
drift with an implementation, and cannot be widened by an issue body -- which
`touches`, parsed out of an attacker-influencable issue, can. Scope drift
keeps everything except the veto: it is still reported, because the exclusion
reasoned on a declaration that turned out to be wrong and the author is the
only one who can fix it.

What still guards an unattended merge is what the design always said guarded
it: a green pipeline on a branch the merge queue rebased, and the fence.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import LABEL_HOLD, IssueState, MergePr, RepoState
from dispatchkit.model import Checks, PullRequest, Verify
from dispatchkit.resolve import Status, merge_ops
from dispatchkit.tick import execute_tick, plan_tick

from .fake_github import FakeGitHub
from .items import issue, item, items_of, ref, state_of

pytestmark = pytest.mark.unit

CONFIG = SchedulerConfig()
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def green(number: int = 12, files: tuple[str, ...] = ("src/a.py",)) -> PullRequest:
    return PullRequest(
        number=number,
        draft=False,
        mergeable=True,
        checks=Checks.PASSING,
        files=files,
    )


def merges(*, touches: tuple[str, ...], files: tuple[str, ...]) -> tuple[MergePr, ...]:
    working = item("a", verify=Verify.AUTO, touches=touches, open_prs=(green(7, files),))
    return merge_ops(items_of(working), CONFIG)


def notices(*issues: IssueState) -> tuple[str, ...]:
    plan = plan_tick(state_of(*issues), config=CONFIG, now=NOW)
    return tuple(str(notice) for notice in plan.notices)


def kinds(*issues: IssueState) -> tuple[str, ...]:
    plan = plan_tick(state_of(*issues), config=CONFIG, now=NOW)
    return tuple(notice.code for notice in plan.notices)


class TestDriftNoLongerWithholdsTheMerge:
    def test_the_live_case_merges(self) -> None:
        # `document-flags`: the brief asked for the test, the block did not
        # admit it. Green, rebased, inside the fence -- it merges.
        assert merges(
            touches=("README.md",), files=("README.md", "tests/test_readme.py")
        ) == (MergePr(ref("a"), 7),)

    def test_an_undeclared_scope_merges(self) -> None:
        # Empty `touches` means "unknown scope", and the design's reading of
        # unknown scope is that it excludes nothing. Reading it here as a
        # refusal made the permissive case the strict one.
        assert merges(touches=(), files=("src/a.py",)) == (MergePr(ref("a"), 7),)

    def test_a_pull_request_inside_its_scope_still_merges(self) -> None:
        assert merges(touches=("src/*",), files=("src/a.py",)) == (MergePr(ref("a"), 7),)


class TestTheFenceStillHoldsAuthority:
    def test_a_fenced_path_is_never_merged_unattended(self) -> None:
        # The pipeline does not rewrite its own workflow, whatever any task
        # declared -- note `touches` here *permits* it and is overruled.
        assert merges(
            touches=(".github/workflows/*",), files=(".github/workflows/ci.yml",)
        ) == ()

    def test_a_fenced_path_among_permitted_ones_still_refuses(self) -> None:
        assert merges(touches=("**",), files=("src/a.py", ".github/workflows/ci.yml")) == ()

    def test_a_graph_file_is_fenced(self) -> None:
        # A plan that could rewrite itself unattended is the fence's whole
        # point, and it is the one path `touches` is most likely to name.
        assert merges(
            touches=("docs/plans/*",), files=("docs/plans/demo.tasks.toml",)
        ) == ()

    def test_an_empty_pull_request_still_refuses(self) -> None:
        working = item(
            "a",
            verify=Verify.AUTO,
            touches=("src/*",),
            open_prs=(PullRequest(7, Checks.PASSING, draft=False, files=(), mergeable=True),),
        )
        assert merge_ops(items_of(working), CONFIG) == ()

    def test_a_conflicting_pull_request_still_refuses(self) -> None:
        working = item(
            "a",
            verify=Verify.AUTO,
            touches=("src/*",),
            open_prs=(
                PullRequest(7, Checks.PASSING, draft=False, files=("src/a.py",), mergeable=False),
            ),
        )
        assert merge_ops(items_of(working), CONFIG) == ()


class TestDriftIsStillReported:
    """Losing the veto is not losing the signal.

    The exclusion admitted this task against a declaration that turned out to
    be wrong, and only the author can correct the graph. Reporting it is also
    what makes the drift countable later, which is the difference between an
    anecdote about one plan and a measurement across many.
    """

    def test_the_drifted_file_is_named(self) -> None:
        reported = notices(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                touches=("README.md",),
                open_prs=(green(12, files=("README.md", "tests/test_readme.py")),),
            )
        )
        assert any("tests/test_readme.py" in line for line in reported)

    def test_it_is_its_own_code_not_a_withheld_merge(self) -> None:
        # The merge is not withheld any more, so filing it under
        # `withheld-merge` would be reporting a refusal that did not happen.
        reported = kinds(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                touches=("README.md",),
                open_prs=(green(12, files=("README.md", "tests/test_readme.py")),),
            )
        )
        assert "scope-drift" in reported
        assert "withheld-merge" not in reported

    def test_it_asks_for_the_graph_to_be_corrected(self) -> None:
        reported = notices(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                touches=("README.md",),
                open_prs=(green(12, files=("README.md", "tests/test_readme.py")),),
            )
        )
        assert any("touches" in line for line in reported)

    def test_a_verify_human_task_drifts_just_as_loudly(self) -> None:
        # The exclusion does not care who merges. A `human` task whose
        # declaration was wrong misinformed the scheduler exactly as much.
        assert "scope-drift" in kinds(
            issue(
                "a",
                number=1,
                verify=Verify.HUMAN,
                touches=("README.md",),
                open_prs=(green(12, files=("README.md", "tests/test_readme.py")),),
            )
        )

    def test_a_pull_request_inside_its_scope_says_nothing(self) -> None:
        assert "scope-drift" not in kinds(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                touches=("README.md",),
                open_prs=(green(12, files=("README.md",)),),
            )
        )

    def test_an_undeclared_scope_says_nothing_here(self) -> None:
        # There is no declaration to have drifted from. A task that should
        # have had one is a question for the authoring lints, asked once,
        # rather than a line in every pass for ever.
        assert "scope-drift" not in kinds(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                touches=(),
                open_prs=(green(12, files=("src/a.py",)),),
            )
        )

    def test_a_pull_request_still_running_ci_says_nothing(self) -> None:
        # An agent that is still pushing has no final file list, and drift
        # measured against a half-written branch is noise.
        pending = PullRequest(
            number=12, draft=False, mergeable=True, checks=Checks.PENDING, files=("src/b.py",)
        )
        assert "scope-drift" not in kinds(
            issue("a", number=1, verify=Verify.AUTO, touches=("src/a.py",), open_prs=(pending,))
        )

    def test_a_held_task_still_reports_drift(self) -> None:
        # A hold says "do not act on this task". The drift notice does not ask
        # anyone to act on the task -- it says the graph is wrong, which is
        # true whether or not the work is paused.
        assert "scope-drift" in kinds(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                labels=("dispatchkit", LABEL_HOLD),
                touches=("README.md",),
                open_prs=(green(12, files=("README.md", "tests/x.py")),),
            )
        )


class TestTheStatusAgrees:
    def test_a_drifting_pull_request_reads_auto_merging_again(self) -> None:
        # It will merge, so saying `In review` would now be the false line.
        plan = plan_tick(
            state_of(
                issue(
                    "a",
                    number=1,
                    verify=Verify.AUTO,
                    touches=("README.md",),
                    open_prs=(green(12, files=("README.md", "tests/test_readme.py")),),
                )
            ),
            config=CONFIG,
            now=NOW,
        )
        assert list(plan.statuses.values()) == [Status.AUTO_MERGING]

    def test_a_fenced_pull_request_is_still_in_review(self) -> None:
        plan = plan_tick(
            state_of(
                issue(
                    "a",
                    number=1,
                    verify=Verify.AUTO,
                    touches=(".github/workflows/*",),
                    open_prs=(green(12, files=(".github/workflows/ci.yml",)),),
                )
            ),
            config=CONFIG,
            now=NOW,
        )
        assert list(plan.statuses.values()) == [Status.IN_REVIEW]


class TestItConverges:
    """The standard for any mutating behaviour: plan, apply, re-read, re-plan.

    Widening what merges widens what could be re-planned, and a merge that
    kept being re-emitted would call `gh pr merge` on a closed pull request
    every pass for ever.
    """

    @staticmethod
    def _state() -> RepoState:
        return state_of(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                assignees=("copilot",),
                touches=("README.md",),
                open_prs=(green(12, files=("README.md", "tests/test_readme.py")),),
            )
        )

    def test_the_drifting_merge_happens_once(self) -> None:
        api = FakeGitHub(state=self._state())
        first = plan_tick(api.state, config=CONFIG, now=NOW)
        assert [op for op in first.operations if isinstance(op, MergePr)]

        execute_tick(first, api)
        second = plan_tick(api.state, config=CONFIG, now=NOW)
        assert second.operations == ()

    def test_the_drift_notice_stops_once_the_task_is_done(self) -> None:
        # A closed task's declaration can no longer be acted on, and the
        # report is a list of things a person can still do something about.
        api = FakeGitHub(state=self._state())
        execute_tick(plan_tick(api.state, config=CONFIG, now=NOW), api)
        second = plan_tick(api.state, config=CONFIG, now=NOW)
        assert "scope-drift" not in [notice.code for notice in second.notices]
        assert list(second.statuses.values()) == [Status.DONE]
