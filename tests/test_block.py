"""D3: the `<!-- dispatchkit ... -->` machine block carried in every issue body.

The block is the scheduler's only structured input, and issue bodies are
attacker-influencable text — an agent, or anyone with write access, can edit
them. So parsing is strict in both directions: a fixed key set, a fixed value
grammar, and a hard refusal when a body carries two blocks rather than
silently trusting the first.
"""

from __future__ import annotations

import pytest

from dispatchkit.block import BLOCK_VERSION, parse_block, render_block
from dispatchkit.errors import GraphError
from dispatchkit.model import Dependency, Lane, Task, TaskId, Verify


def make_task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": TaskId("m5a-values"),
        "title": "Value objects",
        "milestone": "M5a",
        "lane": Lane.CLOUD,
        "acceptance": "uv run pytest -m unit",
        "verify": Verify.AUTO,
        "spend": False,
        "requires": (),
        "touches": ("src/podkit/domain/**",),
        "depends": (Dependency(TaskId("m2-cassette"), "CassetteStore.get() signature"),),
    }
    return Task(**{**base, **overrides})  # type: ignore[arg-type]


def codes(body: str) -> list[str]:
    with pytest.raises(GraphError) as excinfo:
        parse_block(body)
    return [issue.code for issue in excinfo.value.issues]


class TestVersion:
    """`v` is a wire-format version, and it comes first for a reason.

    The block is written into other people's issues, so the only cheap moment
    to add a version key is before any issue exists. Its whole job is to make a
    future incompatible change *fail loudly on the old reader* rather than be
    misread as a valid block with surprising values.
    """

    def test_the_version_is_the_first_key_in_the_block(self) -> None:
        lines = render_block(make_task(), plan="p").splitlines()
        assert lines[1] == f"v: {BLOCK_VERSION}"

    def test_the_parsed_block_carries_the_version_it_was_written_with(self) -> None:
        assert parse_block(render_block(make_task(), plan="p")).version == BLOCK_VERSION

    def test_a_block_from_a_newer_dispatchkit_is_refused_not_guessed_at(self) -> None:
        body = render_block(make_task(), plan="p").replace(
            f"v: {BLOCK_VERSION}", f"v: {BLOCK_VERSION + 1}"
        )
        assert codes(body) == ["block-version"]

    def test_the_refusal_names_both_versions_so_the_fix_is_obvious(self) -> None:
        body = render_block(make_task(), plan="p").replace(f"v: {BLOCK_VERSION}", "v: 99")
        with pytest.raises(GraphError) as excinfo:
            parse_block(body)
        message = excinfo.value.issues[0].message
        assert "99" in message and str(BLOCK_VERSION) in message

    def test_a_newer_block_is_refused_before_its_other_keys_are_read(self) -> None:
        # A newer writer may have changed what any other key means, so this is
        # the one error that must be reported alone rather than in a batch.
        body = render_block(make_task(), plan="p").replace(f"v: {BLOCK_VERSION}", "v: 99")
        assert codes(body.replace("lane: cloud", "lane: root")) == ["block-version"]

    def test_a_block_written_before_versioning_is_rejected_as_incomplete(self) -> None:
        body = render_block(make_task(), plan="p").replace(f"v: {BLOCK_VERSION}\n", "")
        assert codes(body) == ["missing-key"]

    def test_a_non_numeric_version_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace(f"v: {BLOCK_VERSION}", "v: one")
        assert codes(body) == ["invalid-type"]

    def test_a_version_below_the_first_one_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace(f"v: {BLOCK_VERSION}", "v: 0")
        assert codes(body) == ["invalid-type"]


class TestRendering:
    def test_renders_the_documented_shape(self) -> None:
        rendered = render_block(make_task(), plan="refactor")
        assert rendered.splitlines() == [
            "<!-- dispatchkit",
            "v: 1",
            "id: m5a-values",
            "plan: refactor",
            "milestone: M5a",
            "lane: cloud",
            "requires: []",
            "verify: auto",
            "spend: false",
            "depends: [m2-cassette]",
            'touches: ["src/podkit/domain/**"]',
            "-->",
        ]

    def test_renders_booleans_and_empty_lists_unambiguously(self) -> None:
        rendered = render_block(make_task(spend=True, touches=(), depends=()), plan="p")
        assert "spend: true" in rendered
        assert "depends: []" in rendered
        assert "touches: []" in rendered

    def test_quotes_values_that_are_not_plain_slugs(self) -> None:
        rendered = render_block(make_task(milestone="Phase 0"), plan="p")
        assert 'milestone: "Phase 0"' in rendered


class TestRoundTrip:
    def test_block_survives_a_round_trip(self) -> None:
        task = make_task()
        block = parse_block(render_block(task, plan="refactor"))

        assert block.id == task.id
        assert block.plan == "refactor"
        assert block.milestone == task.milestone
        assert block.lane is task.lane
        assert block.verify is task.verify
        assert block.spend is task.spend
        assert block.requires == task.requires
        assert block.touches == task.touches
        assert block.depends == (TaskId("m2-cassette"),)

    def test_round_trips_multiple_dependencies_and_capabilities(self) -> None:
        task = make_task(
            lane=Lane.LOCAL,
            requires=("gpu", "local-data"),
            depends=(
                Dependency(TaskId("a"), "the schema"),
                Dependency(TaskId("b"), "the adapter"),
            ),
        )
        block = parse_block(render_block(task, plan="p"))
        assert block.requires == ("gpu", "local-data")
        assert block.depends == (TaskId("a"), TaskId("b"))

    def test_block_is_found_inside_a_full_issue_body(self) -> None:
        body = "Some prose.\n\n## Acceptance\n\n`true`\n\n" + render_block(make_task(), plan="p")
        assert parse_block(body).id == TaskId("m5a-values")

    def test_rendering_is_stable(self) -> None:
        # `apply` diffs bodies to decide whether to update, so an unstable
        # renderer would rewrite every issue on every run.
        task = make_task()
        assert render_block(task, plan="p") == render_block(task, plan="p")


class TestStrictParsing:
    def test_body_without_a_block_is_an_error(self) -> None:
        assert codes("just prose") == ["block-missing"]

    def test_two_blocks_are_refused_rather_than_resolved(self) -> None:
        body = render_block(make_task(), plan="p") + "\n" + render_block(make_task(), plan="q")
        assert codes(body) == ["block-duplicate"]

    def test_unterminated_block_is_an_error(self) -> None:
        assert codes("<!-- dispatchkit\nid: a\n") == ["block-missing"]

    def test_unknown_key_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace("-->", "owner: mallory\n-->")
        assert codes(body) == ["unknown-key"]

    def test_missing_key_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace("verify: auto\n", "")
        assert codes(body) == ["missing-key"]

    def test_duplicate_key_is_rejected(self) -> None:
        rendered = render_block(make_task(), plan="p")
        body = rendered.replace("lane: cloud", "lane: cloud\nlane: local")
        assert codes(body) == ["duplicate-key"]

    def test_garbage_line_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace("-->", "rm -rf /\n-->")
        assert codes(body) == ["block-syntax"]

    def test_non_slug_id_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace("id: m5a-values", 'id: "../../etc"')
        assert codes(body) == ["invalid-id"]

    def test_non_slug_dependency_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace("[m2-cassette]", '["a; rm -rf /"]')
        assert codes(body) == ["invalid-id"]

    def test_unknown_lane_or_verify_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace("lane: cloud", "lane: root")
        assert codes(body) == ["invalid-enum"]

    def test_non_boolean_spend_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace("spend: false", "spend: yes")
        assert codes(body) == ["invalid-type"]

    def test_unknown_capability_is_rejected(self) -> None:
        body = render_block(make_task(), plan="p").replace("requires: []", "requires: [rootkit]")
        assert codes(body) == ["unknown-capability"]

    def test_a_block_smuggled_into_a_quoted_value_does_not_end_the_block(self) -> None:
        # `touches` is the only free-form field, so it is the injection route.
        task = make_task(touches=('a/-->\n<!-- dispatchkit\nid: evil"',))
        block = parse_block(render_block(task, plan="p"))
        assert block.id == TaskId("m5a-values")
        assert block.touches == task.touches
