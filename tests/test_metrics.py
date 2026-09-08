"""D2: plan-shape metrics over synthetic graphs (chain, star, diamond, singleton).

`width` is the **maximum antichain** — the largest set of tasks that could run
concurrently — not the widest layer of a longest-path layering. The two differ,
and the layered approximation under-reports parallelism, which is exactly the
number this metric exists to expose.
"""

from __future__ import annotations

from dispatchkit.metrics import levels, max_antichain, plan_shape
from dispatchkit.model import Lane, TaskId, Verify
from tests.graphs import chain, diamond, graph, singleton, star, task


class TestLevels:
    def test_chain_levels_increase_by_one(self) -> None:
        assert levels(chain(3)) == {TaskId("t0"): 0, TaskId("t1"): 1, TaskId("t2"): 2}

    def test_level_is_the_longest_path_not_the_shortest(self) -> None:
        # `join` sits at level 2 via left/right even though it also has a
        # direct edge from root; a shortest-path level would say 1.
        g = graph(
            task("root"),
            task("left", depends=("root",)),
            task("join", depends=("root", "left")),
        )
        assert levels(g)[TaskId("join")] == 2


class TestWidth:
    def test_singleton_has_width_one(self) -> None:
        assert max_antichain(singleton()) == (TaskId("a"),)

    def test_chain_has_width_one(self) -> None:
        assert len(max_antichain(chain(5))) == 1

    def test_star_width_is_the_fan_out(self) -> None:
        assert len(max_antichain(star(4))) == 4

    def test_diamond_width_is_two(self) -> None:
        assert set(max_antichain(diamond())) == {TaskId("left"), TaskId("right")}

    def test_width_exceeds_the_widest_layer_when_it_should(self) -> None:
        # a -> b -> c, d -> c, a -> e. Longest-path layering gives layers of
        # size 2, but {b, d, e} are pairwise incomparable, so width is 3.
        g = graph(
            task("a"),
            task("d"),
            task("b", depends=("a",)),
            task("e", depends=("a",)),
            task("c", depends=("b", "d")),
        )
        widest_layer = max(sum(1 for v in levels(g).values() if v == n) for n in range(3))
        assert widest_layer == 2
        assert set(max_antichain(g)) == {TaskId("b"), TaskId("d"), TaskId("e")}

    def test_antichain_members_are_pairwise_independent(self) -> None:
        g = diamond()
        antichain = set(max_antichain(g))
        reachable = g.transitive_prerequisites()
        for node in antichain:
            assert not (reachable[node] & antichain)


class TestPlanShape:
    def test_singleton(self) -> None:
        shape = plan_shape(singleton())
        assert (shape.count, shape.depth, shape.width) == (1, 1, 1)

    def test_chain(self) -> None:
        shape = plan_shape(chain(4))
        assert (shape.count, shape.depth, shape.width) == (4, 4, 1)

    def test_star(self) -> None:
        shape = plan_shape(star(4))
        assert (shape.count, shape.depth, shape.width) == (5, 2, 4)

    def test_diamond(self) -> None:
        shape = plan_shape(diamond())
        assert (shape.count, shape.depth, shape.width) == (4, 3, 2)

    def test_counts_routing_and_verification(self) -> None:
        shape = plan_shape(
            graph(
                task("a", verify=Verify.AUTO),
                task("b", lane=Lane.LOCAL, requires=("gpu",), spend=True),
                task("c"),
            )
        )
        assert (shape.auto_verify, shape.human_verify) == (1, 2)
        assert (shape.cloud, shape.local) == (2, 1)
        assert shape.spend == 1

    def test_review_sessions_batch_concurrent_human_verify_tasks(self) -> None:
        # Two human-verify tasks that can run concurrently are one review
        # sitting; a third behind them is a second.
        g = graph(
            task("root", verify=Verify.AUTO),
            task("left", depends=("root",)),
            task("right", depends=("root",)),
            task("join", depends=("left", "right")),
        )
        assert plan_shape(g).review_sessions == 2

    def test_no_human_verify_means_no_review_sessions(self) -> None:
        g = graph(task("a", verify=Verify.AUTO), task("b", verify=Verify.AUTO))
        assert plan_shape(g).review_sessions == 0

    def test_summary_line_matches_the_documented_format(self) -> None:
        summary = plan_shape(diamond()).summary()
        assert summary == "4 tasks, depth 3, width 2, 4 human-verify, est. 3 review sessions"
