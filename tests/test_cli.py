"""D1 CLI surface: `validate` is the command with the binary exit code that a
human (and later the `apply` step) runs before a graph file is trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.cli import main

VALID = """
[[task]]
id = "a"
title = "A"
milestone = "M"
lane = "cloud"
acceptance = "uv run pytest -m unit"

[[task]]
id = "b"
title = "B"
milestone = "M"
lane = "local"
requires = ["gpu"]
acceptance = "uv run pytest -m unit"
depends = [{ on = "a", for = "the port signature" }]
"""


def test_valid_graph_exits_zero_and_reports_its_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "demo.tasks.toml"
    path.write_text(VALID, encoding="utf-8")

    assert main(["validate", str(path)]) == 0

    out = capsys.readouterr().out
    assert "OK" in out
    assert "2 tasks" in out


def test_invalid_graph_exits_one_and_names_every_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.tasks.toml"
    path.write_text(
        """
        [[task]]
        id = "a"
        title = "A"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"
        depends = [{ on = "ghost", for = "x" }]
        """,
        encoding="utf-8",
    )

    assert main(["validate", str(path)]) == 1

    err = capsys.readouterr().err
    assert "dangling-dependency" in err
    assert "ghost" in err


def test_missing_file_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(tmp_path / "nope.toml")]) == 2
    assert "not found" in capsys.readouterr().err


def test_plan_name_defaults_to_the_filename_stem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "refactor.tasks.toml"
    path.write_text(VALID, encoding="utf-8")

    assert main(["validate", str(path)]) == 0
    assert "refactor" in capsys.readouterr().out


CHAIN = """
[[task]]
id = "a"
title = "A"
milestone = "M"
lane = "cloud"
acceptance = "true"

[[task]]
id = "b"
title = "B"
milestone = "M"
lane = "cloud"
acceptance = "true"
depends = [{ on = "a", for = "the port signature" }]
"""


def test_valid_graph_reports_its_shape_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "demo.tasks.toml"
    path.write_text(VALID, encoding="utf-8")

    assert main(["validate", str(path)]) == 0
    assert "2 tasks, depth 2, width 1, 2 human-verify, est. 2 review sessions" in (
        capsys.readouterr().out
    )


def test_structural_lints_are_warnings_and_do_not_fail_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "chain.tasks.toml"
    path.write_text(CHAIN, encoding="utf-8")

    assert main(["validate", str(path)]) == 0

    out = capsys.readouterr().out
    assert "WARN chain-graph" in out
    assert "OK" in out


def test_lints_can_be_promoted_to_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "chain.tasks.toml"
    path.write_text(CHAIN, encoding="utf-8")

    assert main(["validate", "--strict", str(path)]) == 1
    assert "chain-graph" in capsys.readouterr().err


def test_strict_mode_passes_a_clean_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every task carries a brief, because since D10 one that does not is a
    # lint -- a graph whose agents are dispatched with only a title is not a
    # clean graph, whatever its shape.
    path = tmp_path / "star.tasks.toml"
    path.write_text(
        VALID.replace(
            'acceptance = "uv run pytest -m unit"',
            'acceptance = "uv run pytest -m unit"\nbody_file = "spec.md"',
        )
        + """
[[task]]
id = "c"
title = "C"
milestone = "M"
lane = "cloud"
acceptance = "uv run pytest -m unit"
body_file = "spec.md"
depends = [{ on = "a", for = "the port signature" }]
""",
        encoding="utf-8",
    )
    (tmp_path / "spec.md").write_text("What done looks like, at length.\n", encoding="utf-8")

    assert main(["validate", "--strict", str(path)]) == 0
    assert "WARN" not in capsys.readouterr().out


def write_graph(tmp_path: Path, text: str = VALID, name: str = "demo.tasks.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_apply_is_dry_run_by_default_and_prints_the_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["apply", str(write_graph(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert "dry run" in out
    assert "CreateIssue" in out
    assert "a" in out and "b" in out


def test_apply_refuses_an_invalid_graph_before_touching_github(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = write_graph(
        tmp_path,
        """
        [[task]]
        id = "a"
        title = "A"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"
        depends = [{ on = "ghost", for = "x" }]
        """,
        name="bad.tasks.toml",
    )
    assert main(["apply", str(bad)]) == 1
    assert "dangling-dependency" in capsys.readouterr().err


def test_apply_requires_a_repo_and_project_before_it_will_push(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["apply", "--push", str(write_graph(tmp_path))]) == 2
    assert "--repo" in capsys.readouterr().err


def test_apply_reads_a_body_file_spec_into_the_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "spec.md").write_text("The long-form spec.", encoding="utf-8")
    graph = write_graph(
        tmp_path,
        """
        [[task]]
        id = "a"
        title = "A"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"
        body_file = "spec.md"
        """,
        name="spec.tasks.toml",
    )
    assert main(["apply", "--verbose", str(graph)]) == 0
    assert "The long-form spec." in capsys.readouterr().out


def test_apply_reports_a_missing_body_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = write_graph(
        tmp_path,
        """
        [[task]]
        id = "a"
        title = "A"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"
        body_file = "nope.md"
        """,
        name="spec.tasks.toml",
    )
    assert main(["apply", str(graph)]) == 2
    assert "nope.md" in capsys.readouterr().err


class TestValidateReadsTheBodies:
    """D10: `validate` opens `body_file` so the body contract is checkable.

    The bodies live beside the graph rather than in it, so a lint over them
    only runs if somebody reads them. `apply` already did, to build the issue;
    `validate` did not, which is why the one command whose job is to object
    before anything is written had nothing to say about the part of a task an
    agent actually reads.
    """

    GRAPH = """
[[task]]
id = "a"
title = "A"
milestone = "M"
lane = "cloud"
acceptance = "uv run pytest -m unit"
body_file = "a.md"

[[task]]
id = "b"
title = "B"
milestone = "M"
lane = "cloud"
acceptance = "uv run pytest -m unit"
depends = [{ on = "a", for = "the port" }]

[[task]]
id = "c"
title = "C"
milestone = "M"
lane = "cloud"
acceptance = "uv run pytest -m unit"
depends = [{ on = "a", for = "the port" }]
"""

    def _write(self, tmp_path: Path) -> Path:
        path = tmp_path / "demo.tasks.toml"
        path.write_text(self.GRAPH, encoding="utf-8")
        (tmp_path / "a.md").write_text("Do the thing, thoroughly.\n", encoding="utf-8")
        return path

    def test_a_task_with_no_body_file_is_warned_about(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["validate", str(self._write(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "WARN thin-body: [b]" in out
        assert "WARN thin-body: [c]" in out
        assert "[a]" not in out

    def test_a_body_file_is_resolved_beside_the_graph(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `apply` resolves `body_file` relative to the graph's own directory,
        # and a validator that resolved it against the working directory
        # would disagree with the command that writes the issue.
        nested = tmp_path / "plans"
        nested.mkdir()
        path = nested / "demo.tasks.toml"
        path.write_text(self.GRAPH, encoding="utf-8")
        (nested / "a.md").write_text("Do the thing.\n", encoding="utf-8")

        assert main(["validate", str(path)]) == 0
        assert "thin-body: [a]" not in capsys.readouterr().out

    def test_an_unreadable_body_file_stops_the_command(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A graph pointing at a file that is not there is broken, and finding
        # out at `apply` time is finding out one command too late.
        path = tmp_path / "demo.tasks.toml"
        path.write_text(self.GRAPH, encoding="utf-8")

        assert main(["validate", str(path)]) == 2
        assert "cannot read body_file" in capsys.readouterr().err
