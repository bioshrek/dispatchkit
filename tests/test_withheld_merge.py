"""A `verify: auto` pull request that will not merge has to say why.

Found live, and the way it was found is the point: a green, undrafted,
mergeable pull request sat on the sandbox until someone asked out loud why it
had not merged. Every gate in `merge_ops` had worked exactly as designed — the
agent had touched `tests/test_readme.py` and the graph declared only
`README.md` — and the pass printed nothing at all.

Silence is the wrong output for a withheld merge. `verify: auto` is a promise
that the pipeline decides, so when the pipeline declines, the reason has to
reach the person who can act on it. This is the same rule the cancelled-task
and dirty-plan notices already follow: the design's standard is that a stopped
thing is *named*, never left looking merely unfinished.

Nothing here changes what merges. It changes only what is said about what did
not.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dispatchkit.config import SchedulerConfig
from dispatchkit.github import LABEL_HOLD, IssueState
from dispatchkit.model import Checks, PullRequest, Verify
from dispatchkit.resolve import Status
from dispatchkit.tick import plan_tick

from .items import issue, state_of

pytestmark = pytest.mark.unit

CONFIG = SchedulerConfig()
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def green(number: int = 40, files: tuple[str, ...] = ("src/a.py",)) -> PullRequest:
    return PullRequest(
        number=number,
        draft=False,
        mergeable=True,
        checks=Checks.PASSING,
        files=files,
    )


def notices(*issues: IssueState) -> tuple[str, ...]:
    plan = plan_tick(state_of(*issues), config=CONFIG, now=NOW)
    return tuple(str(notice) for notice in plan.notices)


def kinds(*issues: IssueState) -> tuple[str, ...]:
    plan = plan_tick(state_of(*issues), config=CONFIG, now=NOW)
    return tuple(notice.code for notice in plan.notices)


class TestScopeDrift:
    def test_a_file_the_task_never_declared_is_named(self) -> None:
        # The live case. The body asked the agent for a test; the graph said
        # only `README.md`; the fence held and nobody was told.
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
        assert any("scope-drift" in line for line in reported)

    def test_it_says_what_to_do_about_it(self) -> None:
        # A notice that only reports a refusal leaves the reader to guess
        # which of the two fixes is theirs. Both are named: widen `touches`
        # if the drift was legitimate, or merge it yourself.
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


class TestTheOtherWithheldReasons:
    def test_a_fenced_path_is_named(self) -> None:
        # The pipeline rewriting its own workflow is the case the fence exists
        # for, and the one most likely to be mistaken for a bug in dispatchkit.
        reported = notices(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                touches=(".github/workflows/*", "src/*"),
                open_prs=(green(12, files=(".github/workflows/ci.yml",)),),
            )
        )
        assert any("fenced" in line for line in reported)

    def test_a_conflicting_pull_request_is_named(self) -> None:
        pr = PullRequest(
            number=12, draft=False, mergeable=False, checks=Checks.PASSING, files=("src/a.py",)
        )
        reported = notices(
            issue("a", number=1, verify=Verify.AUTO, touches=("src/*",), open_prs=(pr,))
        )
        assert any("conflict" in line for line in reported)

    def test_a_pull_request_with_no_files_is_named(self) -> None:
        # An empty pull request passes CI trivially, so `pr.files` being empty
        # is a gate rather than a curiosity.
        pr = PullRequest(
            number=12, draft=False, mergeable=True, checks=Checks.PASSING, files=()
        )
        reported = notices(
            issue("a", number=1, verify=Verify.AUTO, touches=("src/*",), open_prs=(pr,))
        )
        assert any("no files" in line for line in reported)


class TestWhatIsDeliberatelyNotReported:
    def test_a_draft_says_nothing_because_the_pass_is_about_to_undraft_it(self) -> None:
        # `ready_ops` takes a green `verify: auto` draft out of draft in this
        # same pass, so a notice would be describing a state that has already
        # ended by the time it is read.
        pr = PullRequest(
            number=12, draft=True, mergeable=True, checks=Checks.PASSING, files=("src/a.py",)
        )
        assert "withheld-merge" not in kinds(
            issue("a", number=1, verify=Verify.AUTO, touches=("src/*",), open_prs=(pr,))
        )

    def test_a_pull_request_still_running_ci_says_nothing(self) -> None:
        # Waiting for checks is the system working. Saying so every pass is
        # how a report becomes something a reader learns to skip.
        pr = PullRequest(
            number=12, draft=False, mergeable=True, checks=Checks.PENDING, files=("src/a.py",)
        )
        assert "withheld-merge" not in kinds(
            issue("a", number=1, verify=Verify.AUTO, touches=("src/*",), open_prs=(pr,))
        )

    def test_a_failing_pull_request_says_nothing_here(self) -> None:
        # A red pull request is already reported by `ci_notices`, and two
        # lines about one thing is worse than one.
        pr = PullRequest(
            number=12, draft=False, mergeable=True, checks=Checks.FAILING, files=("src/a.py",)
        )
        assert "withheld-merge" not in kinds(
            issue("a", number=1, verify=Verify.AUTO, touches=("src/*",), open_prs=(pr,))
        )

    def test_a_verify_human_pull_request_says_nothing(self) -> None:
        # Nobody promised it would merge itself.
        assert "withheld-merge" not in kinds(
            issue(
                "a",
                number=1,
                verify=Verify.HUMAN,
                touches=("README.md",),
                open_prs=(green(12, files=("README.md", "tests/test_readme.py")),),
            )
        )

    def test_a_held_task_says_nothing(self) -> None:
        # A hold is a person saying "not now". Answering it every pass with a
        # reason the merge did not happen argues with them.
        assert "withheld-merge" not in kinds(
            issue(
                "a",
                number=1,
                verify=Verify.AUTO,
                labels=("dispatchkit", LABEL_HOLD),
                touches=("README.md",),
                open_prs=(green(12, files=("README.md", "tests/x.py")),),
            )
        )

    def test_a_dirty_plan_says_only_that(self) -> None:
        # The dirty-plan notice already explains this pass exactly, and it is
        # the more actionable of the two.
        plan = plan_tick(
            state_of(
                issue(
                    "a",
                    number=1,
                    verify=Verify.AUTO,
                    touches=("README.md",),
                    open_prs=(green(12, files=("README.md",)),),
                )
            ),
            config=CONFIG,
            now=NOW,
            dirty=frozenset({"demo"}),
        )
        assert [notice.code for notice in plan.notices].count("dirty-plan") == 1
        assert "withheld-merge" not in [notice.code for notice in plan.notices]


class TestTheStatusAgrees:
    """A withheld merge must not be reported as `Auto-merging`.

    `_status_of` already refuses to say `Auto-merging` over a pipeline that
    cannot decide — a stalled check, a draft, a conflict — for one reason each
    time: the report would be asserting something false. Scope drift and a
    fenced path are the same lie by two more routes. The task is green and
    will sit there for ever, and only a person can move it, which is what
    `In review` means.

    The status and the notice therefore come from one decision rather than
    two, so they cannot drift apart.
    """

    def test_a_drifting_pull_request_is_in_review_not_auto_merging(self) -> None:
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
        assert list(plan.statuses.values()) == [Status.IN_REVIEW]

    def test_a_fenced_pull_request_is_in_review(self) -> None:
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

    def test_a_mergeable_pull_request_is_still_auto_merging(self) -> None:
        plan = plan_tick(
            state_of(
                issue(
                    "a",
                    number=1,
                    verify=Verify.AUTO,
                    touches=("src/*",),
                    open_prs=(green(12, files=("src/a.py",)),),
                )
            ),
            config=CONFIG,
            now=NOW,
        )
        assert list(plan.statuses.values()) == [Status.AUTO_MERGING]

    def test_a_pull_request_still_running_ci_is_auto_merging(self) -> None:
        # The pipeline has not decided yet, and saying so is the whole point
        # of the word. Nothing is withheld — it is simply not finished.
        pr = PullRequest(
            number=12, draft=False, mergeable=True, checks=Checks.PENDING, files=("src/a.py",)
        )
        plan = plan_tick(
            state_of(issue("a", number=1, verify=Verify.AUTO, touches=("src/*",), open_prs=(pr,))),
            config=CONFIG,
            now=NOW,
        )
        assert list(plan.statuses.values()) == [Status.AUTO_MERGING]

    def test_a_dirty_plan_is_still_auto_merging(self) -> None:
        # The gate is this pass only, and the fix is one `git commit`. Calling
        # it `In review` would overstate a condition that clears itself.
        plan = plan_tick(
            state_of(
                issue(
                    "a",
                    number=1,
                    verify=Verify.AUTO,
                    touches=("src/*",),
                    open_prs=(green(12, files=("src/a.py",)),),
                )
            ),
            config=CONFIG,
            now=NOW,
            dirty=frozenset({"demo"}),
        )
        assert list(plan.statuses.values()) == [Status.AUTO_MERGING]
