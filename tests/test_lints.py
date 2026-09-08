"""D2: structural lints over synthetic graphs.

These are **warnings**, not errors — the human ratifies the graph anyway, and
the point is to make that ratification quantitative rather than a vibe check.
So `lint_graph` never raises and never blocks; `validate_graph` (D1) remains
the only thing that can stop a push.
"""

from __future__ import annotations

from dispatchkit.lints import DEFAULT_LINTS, LintConfig, lint_graph
from dispatchkit.model import Lane, Task, TaskGraph, Verify
from tests.graphs import chain, diamond, graph, singleton, star, task


def codes(g: TaskGraph, config: LintConfig = DEFAULT_LINTS) -> list[str]:
    return [issue.code for issue in lint_graph(g, config)]


class TestDecompositionSignals:
    def test_singleton_warns_that_no_decomposition_happened(self) -> None:
        assert "no-decomposition" in codes(singleton())

    def test_chain_is_a_chain_wearing_a_graphs_clothes(self) -> None:
        assert "chain-graph" in codes(chain(4))

    def test_star_is_clean(self) -> None:
        assert codes(star(4)) == []

    def test_diamond_is_clean(self) -> None:
        assert codes(diamond()) == []

    def test_mostly_serial_graph_is_flagged_even_when_not_a_pure_chain(self) -> None:
        # 5 tasks, depth 4: one lone parallel branch does not make it wide.
        g = graph(
            task("a"),
            task("b", depends=("a",)),
            task("c", depends=("b",)),
            task("d", depends=("c",)),
            task("e", depends=("a",)),
        )
        assert "mostly-serial" in codes(g)

    def test_mostly_serial_is_not_reported_on_a_chain(self) -> None:
        # `chain-graph` already says it; two warnings for one fact is noise.
        assert codes(chain(5)) == ["chain-graph"]

    def test_wide_graph_is_not_mostly_serial(self) -> None:
        assert "mostly-serial" not in codes(star(6))


class TestMergeCandidates:
    def _serial_pair(self, b: Task | None = None) -> TaskGraph:
        return graph(task("a"), b or task("b", depends=("a",)), task("c"), task("d"))

    def test_serial_pair_with_identical_routing_is_a_merge_candidate(self) -> None:
        issues = lint_graph(self._serial_pair())
        merge = [i for i in issues if i.code == "merge-candidate"]
        assert len(merge) == 1
        assert "a" in merge[0].message and "b" in merge[0].message

    def test_different_lane_justifies_the_boundary(self) -> None:
        g = self._serial_pair(task("b", depends=("a",), lane=Lane.LOCAL, requires=("gpu",)))
        assert "merge-candidate" not in codes(g)

    def test_different_verify_justifies_the_boundary(self) -> None:
        pair = self._serial_pair(task("b", depends=("a",), verify=Verify.AUTO))
        assert "merge-candidate" not in codes(pair)

    def test_different_spend_justifies_the_boundary(self) -> None:
        pair = self._serial_pair(task("b", depends=("a",), spend=True))
        assert "merge-candidate" not in codes(pair)

    def test_a_shared_prerequisite_is_not_a_merge_candidate(self) -> None:
        # `root` has two dependents, so merging it into either would serialise
        # the other — the fan-out is the whole point.
        assert "merge-candidate" not in codes(star(3))

    def test_a_task_with_two_prerequisites_is_not_a_merge_candidate(self) -> None:
        assert "merge-candidate" not in codes(diamond())


class TestEconomicFloor:
    def test_estimate_below_three_times_overhead_is_flagged(self) -> None:
        g = graph(task("a", estimate_minutes=20), task("b", estimate_minutes=90))
        issues = [i for i in lint_graph(g) if i.code == "under-economic-floor"]
        assert [i.where for i in issues] == ["a"]

    def test_threshold_follows_the_configured_overhead(self) -> None:
        g = graph(task("a", estimate_minutes=20), task("b", estimate_minutes=90))
        config = LintConfig(overhead_minutes=30)  # floor becomes 90 minutes
        assert [i.where for i in lint_graph(g, config) if i.code == "under-economic-floor"] == ["a"]

    def test_missing_estimates_are_silent(self) -> None:
        # Where estimates come from is an open question; absent one, the lint
        # says nothing rather than guessing.
        assert "under-economic-floor" not in codes(graph(task("a"), task("b")))


class TestLintsNeverBlock:
    def test_lints_are_reported_for_a_cyclic_graph_without_raising(self) -> None:
        g = graph(task("a", depends=("b",)), task("b", depends=("a",)))
        assert isinstance(lint_graph(g), list)

    def test_every_lint_names_the_task_or_plan_it_concerns(self) -> None:
        for issue in lint_graph(chain(5)) + lint_graph(singleton()):
            assert issue.where
