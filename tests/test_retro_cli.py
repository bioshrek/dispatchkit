"""D15: the `retro` subcommand.

Read-only, like `resolve`, and for the same reason: there is nothing here that
can be run by accident. It reports and does not gate — feeding an observed
median into `validate` would recreate the lint D2 deleted, with better numbers
and the same failure mode.

The graph file is the argument rather than a plan name, because the promise is
half the report. `metrics.py` already prints what the graph allows; this
prints what happened, and printing an outcome without the expectation beside
it leaves the reader to remember what they were told.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dispatchkit.block import render_block
from dispatchkit.cli import main
from dispatchkit.model import Lane, Task, TaskId, Verify

pytestmark = pytest.mark.unit

PLAN = "demo"

GRAPH = """
plan = "demo"

[[task]]
id = "one"
title = "One"
milestone = "M"
lane = "cloud"
verify = "auto"
acceptance = "true"
body_file = "one.md"

[[task]]
id = "two"
title = "Two"
milestone = "M"
lane = "cloud"
verify = "auto"
acceptance = "true"
body_file = "two.md"
"""


def write_graph(tmp_path: Path) -> Path:
    path = tmp_path / "demo.tasks.toml"
    path.write_text(GRAPH, encoding="utf-8")
    for name in ("one", "two"):
        (tmp_path / f"{name}.md").write_text(
            f"# {name}\n\nDo the {name} thing.\n", encoding="utf-8"
        )
    return path


def issue_node(
    name: str,
    number: int,
    *,
    dispatched: str | None = None,
    first_commit: str | None = None,
    ci: tuple[str, str] | None = None,
    closed_at: str | None = None,
) -> dict[str, Any]:
    task = Task(
        id=TaskId(name),
        title=name,
        milestone="M",
        lane=Lane.CLOUD,
        verify=Verify.AUTO,
        spend=False,
        acceptance="true",
        depends=(),
        touches=(),
        requires=(),
    )
    source: dict[str, Any] = {
        "number": number + 100,
        "state": "MERGED",
        "baseRefName": "main",
    }
    if first_commit:
        source["first_commit"] = {"nodes": [{"commit": {"committedDate": first_commit}}]}
    if ci:
        source["commits"] = {
            "nodes": [
                {
                    "commit": {
                        "checkSuites": {
                            "nodes": [
                                {
                                    "status": "COMPLETED",
                                    "conclusion": "SUCCESS",
                                    "createdAt": ci[0],
                                    "updatedAt": ci[1],
                                }
                            ]
                        }
                    }
                }
            ]
        }
    return {
        "number": number,
        "title": name,
        "body": render_block(task, plan=PLAN),
        "state": "CLOSED" if closed_at else "OPEN",
        "closedAt": closed_at,
        "labels": {"nodes": [{"name": "dispatchkit"}]},
        "assignees": {"nodes": []},
        "timelineItems": {"nodes": [{"source": source}] if first_commit or ci else []},
        "dispatches": {
            "nodes": (
                [{"createdAt": dispatched, "assignee": {"login": "copilot-swe-agent"}}]
                if dispatched
                else []
            )
        },
    }


def finished(name: str, number: int, *, start: int, end: int) -> dict[str, Any]:
    """A task dispatched at `start` and closed at `end`, both whole hours."""
    return issue_node(
        name,
        number,
        dispatched=f"2026-09-10T{start:02d}:00:00Z",
        first_commit=f"2026-09-10T{start:02d}:06:00Z",
        ci=(f"2026-09-10T{start:02d}:10:00Z", f"2026-09-10T{start:02d}:14:00Z"),
        closed_at=f"2026-09-10T{end:02d}:00:00Z",
    )


def write_state(tmp_path: Path, *nodes: dict[str, Any]) -> Path:
    path = tmp_path / "state.json"
    payload = {"data": {"repository": {"issues": {"nodes": list(nodes)}}}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def run(tmp_path: Path, *nodes: dict[str, Any]) -> int:
    return main(
        [
            "retro",
            str(write_graph(tmp_path)),
            "--state",
            str(write_state(tmp_path, *nodes)),
        ]
    )


class TestTheReport:
    def test_it_names_the_plan_and_counts_what_it_measured(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(tmp_path, finished("one", 1, start=9, end=11)) == 0
        out = capsys.readouterr().out

        assert "demo" in out
        assert "1 measured" in out

    def test_it_prints_a_line_per_task_with_both_durations(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(tmp_path, finished("one", 1, start=9, end=11))
        out = capsys.readouterr().out

        assert "one" in out
        # overhead 6m to first commit + 4m of CI; work 2h.
        assert "10m" in out
        assert "2h" in out

    def test_it_reports_the_median_overhead_that_replaces_the_guess(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(tmp_path, finished("one", 1, start=9, end=11), finished("two", 2, start=9, end=12))
        out = capsys.readouterr().out

        assert "median overhead" in out
        assert "10m" in out

    def test_it_puts_the_promised_width_beside_the_achieved_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The comparison is the whole point of taking a graph file."""
        run(tmp_path, finished("one", 1, start=9, end=10), finished("two", 2, start=11, end=12))
        out = capsys.readouterr().out

        assert "promised 2" in out
        assert "achieved 1" in out

    def test_it_says_which_tasks_did_not_earn_their_dispatch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(tmp_path, finished("one", 1, start=9, end=11), finished("two", 2, start=9, end=12))
        out = capsys.readouterr().out

        assert "below the floor" in out

    def test_it_reports_unmeasured_tasks_rather_than_hiding_them(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A retrospective covering one of two tasks is a different claim."""
        run(tmp_path, finished("one", 1, start=9, end=11), issue_node("two", 2))
        out = capsys.readouterr().out

        assert "1 unmeasured" in out


class TestWhenThereIsNothingToSay:
    def test_a_plan_with_no_issues_says_so_rather_than_printing_zeroes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(tmp_path) == 0

        assert "no issues" in capsys.readouterr().out

    def test_a_plan_nothing_can_be_measured_from_still_reports(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unfinished is not an error; it is early.

        A plan halfway through has a real partial answer, and refusing to
        print it would make the tool useless exactly when someone is deciding
        whether the decomposition is working.
        """
        assert run(tmp_path, issue_node("one", 1)) == 0
        out = capsys.readouterr().out

        assert "1 unmeasured" in out
        assert "median overhead" not in out


class TestItRefusesToGuess:
    def test_a_dry_run_needs_a_snapshot_or_a_repository(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["retro", str(write_graph(tmp_path))])

        assert code != 0
        assert "--state" in capsys.readouterr().err

    def test_an_unreadable_graph_is_reported_not_ignored(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "nope.tasks.toml"
        code = main(["retro", str(missing), "--state", str(write_state(tmp_path))])

        assert code != 0
        assert "not found" in capsys.readouterr().err


class TestItDoesNotLetTheNumbersMislead:
    def test_it_names_the_lane_on_every_task(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Because the overhead figure is only comparable within a lane."""
        run(tmp_path, finished("one", 1, start=9, end=11))

        assert "cloud" in capsys.readouterr().out

    def test_it_says_what_makes_the_overhead_lane_shaped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Found live, and the most important thing this report has to say.

        A cloud agent creates its branch and commits within seconds of being
        assigned, so dispatch-to-first-commit is near zero however long the
        task then takes. A local run commits once, when it finishes, so the
        same span is nearly the whole task. The number is real in both cases
        and means the opposite thing, and a reader given it without that
        warning will conclude the cloud lane has no overhead.
        """
        run(tmp_path, finished("one", 1, start=9, end=11))
        out = capsys.readouterr().out

        assert "lane" in out
        assert "compare within a lane" in out
