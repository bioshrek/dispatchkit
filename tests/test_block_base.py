"""D16: the base travels in the machine block, at block version 2.

The scheduler reads issues, not files. `watch` is allowed to run on a machine
with no checkout — `workstation_cli` says so explicitly — so anything a pass
needs must be on GitHub, and the machine block is where per-task facts already
live. The base joins `plan` and `milestone` as plan-level data repeated into
every issue of the plan.

The grammar cannot express a key an old reader ignores, so this bumps the
version. That is cheaper than it looks: only a *future* version is refused, so
a v1 block still reads — and a v1 block was written before plan branches
existed, which means its plan integrates on `main`. The default and the
backward-compatible reading are the same value, so there is no migration.
"""

from __future__ import annotations

import pytest

from dispatchkit.block import BLOCK_VERSION, parse_block, render_block
from dispatchkit.model import DEFAULT_BASE, Base, Lane, Task, TaskId, Verify

pytestmark = pytest.mark.unit

TASK = Task(
    id=TaskId("version-flag"),
    title="Report the package version",
    milestone="M1",
    lane=Lane.LOCAL,
    acceptance="make check",
    verify=Verify.AUTO,
)


def rendered(base: Base) -> str:
    return render_block(TASK, plan="wordfreq", base=base)


class TestTheVersionMoved:
    def test_it_is_two(self) -> None:
        # A key the grammar cannot make optional is a new wire format.
        assert BLOCK_VERSION == 2


class TestTheBaseRoundTrips:
    def test_an_explicit_base_survives(self) -> None:
        block = parse_block(rendered(Base("plan/wordfreq")))
        assert block.base == Base("plan/wordfreq")

    def test_the_default_survives(self) -> None:
        block = parse_block(rendered(DEFAULT_BASE))
        assert block.base == DEFAULT_BASE

    def test_it_is_rendered_as_a_key(self) -> None:
        assert "base: plan/wordfreq" in rendered(Base("plan/wordfreq"))


class TestAVersionOneBlock:
    """Written before plan branches existed, so it integrates on `main`."""

    V1 = """<!-- dispatchkit
v: 1
id: gpu
plan: demo
milestone: M1
lane: local
requires: []
verify: human
spend: false
depends: []
touches: []
-->"""

    def test_it_still_reads(self) -> None:
        assert parse_block(self.V1).id == TaskId("gpu")

    def test_its_base_is_main(self) -> None:
        assert parse_block(self.V1).base == DEFAULT_BASE


class TestABadBaseInABody:
    """Issue bodies are attacker-influencable; this one arrives as a ref."""

    def body(self, value: str) -> str:
        return rendered(DEFAULT_BASE).replace("base: main", f"base: {value}")

    def test_an_option_is_refused(self) -> None:
        from dispatchkit.errors import GraphError

        with pytest.raises(GraphError):
            parse_block(self.body('"-x"'))

    def test_a_traversal_is_refused(self) -> None:
        from dispatchkit.errors import GraphError

        with pytest.raises(GraphError):
            parse_block(self.body('"a..b"'))
