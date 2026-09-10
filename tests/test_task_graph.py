"""D1: task graph schema + validator (parse, cycle, dangling).

Per `docs/automation_task_dispatch_requirements.md`'s build order, D1 is
proven by unit tests with no network: the graph file is the reviewable unit,
so every invariant it claims — unique ids, resolvable `depends`, acyclicity,
a non-empty `for` on every edge, non-empty `acceptance`, and the
capability-based lane routing rule — must fail loudly *before* anything is
pushed to GitHub.
"""

from __future__ import annotations

import pytest

from dispatchkit.model import Lane, TaskId, Verify
from dispatchkit.parse import GraphError, parse_graph
from dispatchkit.validate import validate_graph

MINIMAL = """
[[task]]
id = "m1-ports"
title = "Define ports"
milestone = "M1"
lane = "cloud"
acceptance = "uv run pytest -m unit"
"""


def codes(text: str, *, plan: str = "demo") -> list[str]:
    """Parse+validate `text`, returning the issue codes it reports."""
    try:
        graph = parse_graph(text, plan=plan)
    except GraphError as exc:
        return [issue.code for issue in exc.issues]
    return [issue.code for issue in validate_graph(graph)]


class TestParsing:
    def test_parses_minimal_task_and_applies_defaults(self) -> None:
        graph = parse_graph(MINIMAL, plan="demo")
        (task,) = graph.tasks

        assert graph.plan == "demo"
        assert task.id == TaskId("m1-ports")
        assert task.title == "Define ports"
        assert task.milestone == "M1"
        assert task.lane is Lane.CLOUD
        assert task.verify is Verify.HUMAN  # human is the default; auto is opt-in
        assert task.spend is False
        assert task.requires == ()
        assert task.touches == ()
        assert task.depends == ()
        assert task.body_file is None
        assert validate_graph(graph) == []

    def test_parses_every_documented_field(self) -> None:
        graph = parse_graph(
            """
            [[task]]
            id = "m2-cassette"
            title = "Cassette store"
            milestone = "M2"
            lane = "local"
            requires = ["local-data", "long-run"]
            verify = "auto"
            spend = true
            touches = ["src/podkit/infrastructure/**"]
            acceptance = "uv run pytest -m replay"
            body_file = "docs/plans/m2-cassette.md"

            [[task]]
            id = "m5a-values"
            title = "Value objects"
            milestone = "M5a"
            lane = "cloud"
            acceptance = "uv run pytest -m unit"
            depends = [{ on = "m2-cassette", for = "CassetteStore.get() signature" }]
            """,
            plan="refactor",
        )
        first, second = graph.tasks

        assert first.requires == ("local-data", "long-run")
        assert first.verify is Verify.AUTO
        assert first.spend is True
        assert first.touches == ("src/podkit/infrastructure/**",)
        assert first.body_file == "docs/plans/m2-cassette.md"
        assert second.depends[0].on == TaskId("m2-cassette")
        assert second.depends[0].reason == "CassetteStore.get() signature"
        assert validate_graph(graph) == []

    def test_the_estimate_key_is_gone(self) -> None:
        """D10: `estimate_minutes` left the schema rather than being ignored.

        An unreviewed guess at a duration is the one number in the graph
        nobody could check, and the D2 economic-floor lint it fed was
        therefore reasoning from a number the author had made up. D15 measures
        the same thing from the timeline instead. Rejecting the key rather
        than tolerating it means a plan carrying one is told, once, rather
        than quietly having its estimates ignored for ever.
        """
        assert codes(MINIMAL + "\nestimate_minutes = 45\n") == ["unknown-key"]

    def test_optional_top_level_plan_key_overrides_the_filename_stem(self) -> None:
        graph = parse_graph('plan = "refactor"\n' + MINIMAL, plan="demo")
        assert graph.plan == "refactor"

    def test_rejects_malformed_toml(self) -> None:
        assert codes("[[task]\nid = ") == ["invalid-toml"]

    def test_rejects_a_file_with_no_tasks(self) -> None:
        assert codes("") == ["empty-graph"]

    def test_rejects_unknown_task_key(self) -> None:
        assert codes(MINIMAL + '\nowner = "me"\n') == ["unknown-key"]

    def test_rejects_unknown_top_level_key(self) -> None:
        assert codes('project = "board"\n' + MINIMAL) == ["unknown-key"]

    @pytest.mark.parametrize("key", ["id", "title", "milestone", "lane", "acceptance"])
    def test_rejects_missing_required_key(self, key: str) -> None:
        text = "\n".join(line for line in MINIMAL.splitlines() if not line.startswith(f"{key} "))
        assert "missing-key" in codes(text)

    def test_rejects_wrong_types(self) -> None:
        assert codes(MINIMAL + "\nspend = 1\n") == ["invalid-type"]
        assert codes(MINIMAL + '\ntouches = "src/**"\n') == ["invalid-type"]
        assert codes(MINIMAL + "\nrequires = [3]\n") == ["invalid-type"]

    def test_rejects_unknown_lane_and_verify_values(self) -> None:
        assert codes(MINIMAL.replace('lane = "cloud"', 'lane = "hybrid"')) == ["invalid-enum"]
        assert codes(MINIMAL + '\nverify = "maybe"\n') == ["invalid-enum"]

    def test_rejects_a_dependency_edge_that_is_not_a_table(self) -> None:
        assert codes(MINIMAL + '\ndepends = ["m2-cassette"]\n') == ["invalid-type"]

    def test_rejects_an_edge_with_an_unknown_key(self) -> None:
        assert codes(MINIMAL + '\ndepends = [{ on = "x", for = "y", why = "z" }]\n') == [
            "unknown-key"
        ]

    def test_collects_every_schema_problem_in_one_pass(self) -> None:
        # A reviewer should see the whole list, not the first failure.
        assert sorted(codes(MINIMAL + '\nowner = "me"\nspend = 1\nverify = "maybe"\n')) == [
            "invalid-enum",
            "invalid-type",
            "unknown-key",
        ]


class TestGraphInvariants:
    def test_rejects_duplicate_ids(self) -> None:
        assert codes(MINIMAL + MINIMAL) == ["duplicate-id"]

    @pytest.mark.parametrize("bad", ["M1 Ports", "m1_ports", "-m1", "m1--ports", ""])
    def test_rejects_non_slug_ids(self, bad: str) -> None:
        assert "invalid-id" in codes(MINIMAL.replace('id = "m1-ports"', f'id = "{bad}"'))

    def test_rejects_a_dangling_dependency(self) -> None:
        issues = validate_graph(
            parse_graph(
                MINIMAL + '\ndepends = [{ on = "nope", for = "the schema" }]\n',
                plan="demo",
            )
        )
        (issue,) = issues
        assert issue.code == "dangling-dependency"
        assert "nope" in issue.message

    def test_rejects_a_self_dependency(self) -> None:
        assert codes(MINIMAL + '\ndepends = [{ on = "m1-ports", for = "itself" }]\n') == [
            "self-dependency"
        ]

    def test_rejects_a_duplicate_edge_to_the_same_task(self) -> None:
        text = """
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
        depends = [{ on = "a", for = "x" }, { on = "a", for = "y" }]
        """
        assert codes(text) == ["duplicate-dependency"]

    def test_rejects_an_edge_with_an_empty_reason(self) -> None:
        # An edge that cannot name what it waits on is a spurious edge.
        assert codes(MINIMAL + '\ndepends = [{ on = "m1-ports", for = "  " }]\n') == [
            "empty-edge-reason",
            "self-dependency",
        ]

    def test_rejects_an_edge_missing_its_reason(self) -> None:
        assert codes(MINIMAL + '\ndepends = [{ on = "m1-ports" }]\n') == ["missing-key"]

    def test_rejects_empty_acceptance(self) -> None:
        text = MINIMAL.replace('acceptance = "uv run pytest -m unit"', 'acceptance = " "')
        assert codes(text) == ["empty-acceptance"]

    def test_rejects_empty_title_and_milestone(self) -> None:
        assert codes(MINIMAL.replace('title = "Define ports"', 'title = ""')) == ["empty-field"]
        assert codes(MINIMAL.replace('milestone = "M1"', 'milestone = ""')) == ["empty-field"]


class TestCycles:
    def _chain(self, edges: dict[str, str]) -> str:
        blocks = []
        for node in sorted({*edges, *edges.values()}):
            dep = f'\ndepends = [{{ on = "{edges[node]}", for = "x" }}]' if node in edges else ""
            blocks.append(
                f'[[task]]\nid = "{node}"\ntitle = "{node}"\nmilestone = "M"\n'
                f'lane = "cloud"\nacceptance = "true"{dep}\n'
            )
        return "\n".join(blocks)

    def test_two_cycle_is_a_hard_error_naming_the_cycle(self) -> None:
        issues = validate_graph(parse_graph(self._chain({"a": "b", "b": "a"}), plan="demo"))
        (issue,) = issues
        assert issue.code == "cycle"
        assert "a" in issue.message and "b" in issue.message

    def test_three_cycle_is_detected(self) -> None:
        assert codes(self._chain({"a": "c", "b": "a", "c": "b"})) == ["cycle"]

    def test_diamond_is_acyclic_and_orders_topologically(self) -> None:
        text = """
        [[task]]
        id = "root"
        title = "root"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"

        [[task]]
        id = "left"
        title = "left"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"
        depends = [{ on = "root", for = "schema" }]

        [[task]]
        id = "right"
        title = "right"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"
        depends = [{ on = "root", for = "schema" }]

        [[task]]
        id = "join"
        title = "join"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"
        depends = [
          { on = "left", for = "adapter" },
          { on = "right", for = "adapter" },
        ]
        """
        graph = parse_graph(text, plan="demo")
        assert validate_graph(graph) == []

        order = graph.topological_order()
        assert order.index(TaskId("root")) < order.index(TaskId("left"))
        assert order.index(TaskId("left")) < order.index(TaskId("join"))
        assert order.index(TaskId("right")) < order.index(TaskId("join"))

    def test_topological_order_is_deterministic_in_file_order(self) -> None:
        graph = parse_graph(self._chain({"b": "a"}) + self._chain({}), plan="demo")
        assert graph.topological_order() == (TaskId("a"), TaskId("b"))

    def test_topological_order_raises_on_a_cyclic_graph(self) -> None:
        graph = parse_graph(self._chain({"a": "b", "b": "a"}), plan="demo")
        with pytest.raises(GraphError):
            graph.topological_order()


class TestLaneRouting:
    def test_cloud_lane_requires_no_capabilities(self) -> None:
        issues = validate_graph(parse_graph(MINIMAL + '\nrequires = ["gpu"]\n', plan="demo"))
        (issue,) = issues
        assert issue.code == "lane-capability-mismatch"
        assert "gpu" in issue.message

    def test_local_lane_may_declare_capabilities(self) -> None:
        text = MINIMAL.replace('lane = "cloud"', 'lane = "local"') + '\nrequires = ["gpu"]\n'
        assert codes(text) == []

    def test_local_lane_with_no_capabilities_is_allowed(self) -> None:
        # Routing says cloud *may* take an untagged task, not that it must.
        assert codes(MINIMAL.replace('lane = "cloud"', 'lane = "local"')) == []

    def test_rejects_an_unknown_capability_tag(self) -> None:
        text = MINIMAL.replace('lane = "cloud"', 'lane = "local"') + '\nrequires = ["quantum"]\n'
        assert codes(text) == ["unknown-capability"]

    def test_rejects_a_duplicate_capability_tag(self) -> None:
        text = MINIMAL.replace('lane = "cloud"', 'lane = "local"') + '\nrequires = ["gpu", "gpu"]\n'
        assert codes(text) == ["duplicate-capability"]


class TestMultipleIssuesAreReported:
    def test_validation_reports_every_problem_at_once(self) -> None:
        text = """
        [[task]]
        id = "a"
        title = "A"
        milestone = "M"
        lane = "cloud"
        requires = ["gpu"]
        acceptance = " "

        [[task]]
        id = "b"
        title = "B"
        milestone = "M"
        lane = "cloud"
        acceptance = "true"
        depends = [{ on = "ghost", for = "x" }]
        """
        assert sorted(codes(text)) == [
            "dangling-dependency",
            "empty-acceptance",
            "lane-capability-mismatch",
        ]

    def test_issues_name_the_task_they_belong_to(self) -> None:
        graph = parse_graph(MINIMAL + '\nrequires = ["gpu"]\n', plan="demo")
        (issue,) = validate_graph(graph)
        assert issue.where == "m1-ports"
