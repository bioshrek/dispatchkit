"""D11: the fence has to contain itself.

`default_fence` is careful. Its docstring argues that both documented config
locations are fenced "not just the one in use: a config that moves house must
not be able to arrive unreviewed", and it fences the workflows and the plans
glob for the same reason. All of that reasoning is thrown away the moment
somebody writes `fence.paths` in their config, because an explicit list
replaces the default outright.

That is the classic shape -- a safe default with an unguarded override -- and
after D9.2 it matters more than it did, because demoting `touches` left the
fence as the *only* permission boundary in the system.

The escalation is short and real. A `verify: auto` pull request that edits
`.github/dispatchkit.toml` is unfenced under `fence.paths = ["src/**"]`, so
`merge_ops` merges it on green CI; the next pass loads the new config; and
`runner.argv` is a command `watch --local` executes on the maintainer's
machine. Rewriting the workflows attacks what CI means, and rewriting the
plans glob attacks the reviewed artifact itself.

So this is not advice, and it is not a lint on somebody's plan -- it is
`doctor` refusing to call a repository healthy while the thing that governs
the pipeline is something the pipeline may rewrite unattended.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatchkit.config import DEFAULT_LOCATIONS, SchedulerConfig, default_fence
from dispatchkit.doctor import check_fence

pytestmark = pytest.mark.unit

CONFIG = Path(".github/dispatchkit.toml")
PLANS = Path("docs/plans")


def fenced(*patterns: str) -> SchedulerConfig:
    return SchedulerConfig(fence=tuple(patterns), plans=PLANS)


class TestTheDefaultIsHealthy:
    def test_the_fence_it_derives_protects_everything_it_should(self) -> None:
        config = SchedulerConfig(fence=default_fence(CONFIG, PLANS), plans=PLANS)

        assert check_fence(config, CONFIG).ok

    def test_the_other_documented_config_location_is_also_healthy(self) -> None:
        other = DEFAULT_LOCATIONS[1]
        config = SchedulerConfig(fence=default_fence(other, PLANS), plans=PLANS)

        assert check_fence(config, other).ok


class TestAnEmptyFence:
    def test_it_is_refused(self) -> None:
        # `fence.paths = []` parses, and disables the only boundary there is.
        assert not check_fence(fenced(), CONFIG).ok

    def test_it_says_the_fence_is_empty_rather_than_listing_everything(self) -> None:
        detail = check_fence(fenced(), CONFIG).detail

        assert "empty" in detail


class TestAFenceThatDoesNotContainItself:
    def test_an_unprotected_config_is_refused(self) -> None:
        check = check_fence(
            fenced("src/**", ".github/workflows/**", "docs/plans/*.tasks.toml"), CONFIG
        )

        assert not check.ok
        assert ".github/dispatchkit.toml" in check.detail

    def test_unprotected_workflows_are_refused(self) -> None:
        # CI is what `verify: auto` trusts. An agent that may rewrite the
        # workflow may decide what green means.
        check = check_fence(fenced(".github/dispatchkit.toml", "docs/plans/*.tasks.toml"), CONFIG)

        assert not check.ok
        assert ".github/workflows" in check.detail

    def test_an_unprotected_plans_glob_is_refused(self) -> None:
        # The graph is the reviewed artifact. Rewriting it unattended makes
        # the review meaningless.
        check = check_fence(fenced(".github/dispatchkit.toml", ".github/workflows/**"), CONFIG)

        assert not check.ok
        assert "docs/plans" in check.detail

    def test_every_gap_is_named_at_once(self) -> None:
        # Not one at a time. Somebody fixing a fence should get the whole list.
        check = check_fence(fenced("src/**"), CONFIG)

        assert ".github/dispatchkit.toml" in check.detail
        assert ".github/workflows" in check.detail
        assert "docs/plans" in check.detail

    def test_the_remedy_names_the_key_to_edit(self) -> None:
        assert "fence.paths" in check_fence(fenced("src/**"), CONFIG).remedy


class TestItFollowsTheConfiguration:
    def test_a_relocated_plans_directory_is_the_one_checked(self) -> None:
        config = SchedulerConfig(
            fence=(".github/dispatchkit.toml", ".github/workflows/**", "docs/plans/*.tasks.toml"),
            plans=Path("tasks"),
        )

        check = check_fence(config, CONFIG)

        assert not check.ok
        assert "tasks" in check.detail

    def test_a_config_read_from_elsewhere_is_the_one_checked(self) -> None:
        # `--config ops/dk.toml` moves the file the pipeline is governed by,
        # so that is the path the fence has to cover.
        check = check_fence(
            fenced(".github/workflows/**", "docs/plans/*.tasks.toml"), Path("ops/dk.toml")
        )

        assert not check.ok
        assert "ops/dk.toml" in check.detail

    def test_a_wide_pattern_covers_it(self) -> None:
        # The check asks whether a representative path is fenced, not whether
        # the pattern is spelled a particular way. `.github/**` covers both
        # the config and the workflows.
        assert check_fence(fenced(".github/**", "docs/**"), CONFIG).ok


class TestItIsWiredIn:
    def test_the_local_checks_include_it(self) -> None:
        from dispatchkit.doctor import LocalFacts, check_local

        facts = LocalFacts(
            config_path=CONFIG,
            config_exists=True,
            plans=PLANS,
            plans_exists=True,
            config=fenced("src/**"),
        )

        assert "fence" in {check.name for check in check_local(facts)}
        assert not all(check.ok for check in check_local(facts))
