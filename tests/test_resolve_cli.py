"""D4: the `resolve` subcommand.

Read-only by construction — it prints what the scheduler *would* see. Mutation
(assigning, commenting, writing the board) is D5's job, so there is nothing
here that can be run by accident.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatchkit.block import render_block
from dispatchkit.cli import main
from dispatchkit.model import Lane, Task, TaskId, Verify

PLAN = "demo"


def issue_node(
    name: str,
    number: int,
    *,
    depends: tuple[str, ...] = (),
    closed: bool = False,
    assignees: tuple[str, ...] = (),
    status: str = "Blocked",
) -> dict[str, object]:
    task = Task(
        id=TaskId(name),
        title=name,
        milestone="M",
        lane=Lane.CLOUD,
        verify=Verify.HUMAN,
        spend=False,
        acceptance="true",
        depends=(),
        touches=(),
        requires=(),
    )
    body = render_block(task, plan=PLAN)
    # `depends` is easier to inject textually than to rebuild the Task for.
    if depends:
        rendered = "[" + ", ".join(depends) + "]"
        body = body.replace("depends: []", f"depends: {rendered}")
    return {
        "number": number,
        "title": name,
        "body": body,
        "state": "CLOSED" if closed else "OPEN",
        "labels": {"nodes": [{"name": "dispatchkit"}]},
        "assignees": {"nodes": [{"login": login} for login in assignees]},
        "timelineItems": {"nodes": []},
        "projectItems": {
            "nodes": [
                {
                    "id": f"PVTI_{number}",
                    "fieldValues": {
                        "nodes": [{"name": status, "field": {"name": "Status"}}],
                    },
                }
            ]
        },
    }


def write_state(tmp_path: Path, *nodes: dict[str, object]) -> Path:
    path = tmp_path / "state.json"
    payload = {"data": {"repository": {"issues": {"nodes": list(nodes)}}}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestResolveCommand:
    def test_it_prints_a_status_for_every_task(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = write_state(
            tmp_path,
            issue_node("a", 1, closed=True),
            issue_node("b", 2, depends=("a",)),
        )
        assert main(["resolve", "--state", str(state), "--plan", PLAN]) == 0
        out = capsys.readouterr().out
        assert "a" in out and "Done" in out
        assert "Ready" in out

    def test_it_reports_what_it_would_admit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = write_state(tmp_path, issue_node("a", 1))
        main(["resolve", "--state", str(state), "--plan", PLAN])
        assert "admit: a" in capsys.readouterr().out

    def test_it_reports_board_writes_it_would_make(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = write_state(tmp_path, issue_node("a", 1, status="Blocked"))
        main(["resolve", "--state", str(state), "--plan", PLAN])
        assert "Status=Ready" in capsys.readouterr().out

    def test_it_says_so_when_the_board_already_agrees(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = write_state(tmp_path, issue_node("a", 1, status="Ready"))
        main(["resolve", "--state", str(state), "--plan", PLAN])
        assert "board is up to date" in capsys.readouterr().out

    def test_it_explains_deferrals(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = write_state(
            tmp_path,
            *(issue_node(f"t{i}", i, assignees=("copilot",)) for i in range(1, 4)),
            issue_node("t4", 4),
        )
        main(["resolve", "--state", str(state), "--plan", PLAN])
        assert "lane-cap" in capsys.readouterr().out

    def test_a_plan_with_no_matching_issues_is_not_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = write_state(tmp_path, issue_node("a", 1))
        assert main(["resolve", "--state", str(state), "--plan", "other"]) == 0
        assert "no issues" in capsys.readouterr().out


class TestResolveFailures:
    def test_a_missing_state_file_is_an_operator_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["resolve", "--state", str(tmp_path / "nope.json"), "--plan", PLAN]) == 2
        assert "cannot read" in capsys.readouterr().err

    def test_unreadable_json_is_an_operator_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text("{oops", encoding="utf-8")
        assert main(["resolve", "--state", str(path), "--plan", PLAN]) == 2

    def test_a_bad_config_is_reported_not_ignored(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "dispatchkit.toml"
        config.write_text("[caps]\ncoud = 3\n", encoding="utf-8")
        state = write_state(tmp_path, issue_node("a", 1))
        code = main(
            ["resolve", "--state", str(state), "--plan", PLAN, "--config", str(config)]
        )
        assert code == 1
        assert "invalid-enum" in capsys.readouterr().err

    def test_an_unreadable_issue_body_is_a_notice_not_a_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broken = issue_node("a", 1)
        broken["body"] = "<!-- dispatchkit\nnonsense\n-->"
        state = write_state(tmp_path, broken)
        assert main(["resolve", "--state", str(state), "--plan", PLAN]) == 0
        assert "unreadable-issue" in capsys.readouterr().out
