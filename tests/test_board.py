"""D5.5: the board contract — what a project must look like to be usable.

The board is a derived view, but it is a *typed* derived view: writing a status
the board has no option for fails at the API, halfway through a pass. So the
set of fields, their kinds and their option lists are stated once, here, and
both `doctor` and `init` read them from the same place.

The tests that matter are the ones tying this contract to the code that writes
to it: every value the scheduler can produce must be a value the board accepts.
"""

from __future__ import annotations

import pytest

from dispatchkit.board import (
    REQUIRED_FIELDS,
    REQUIRED_LABELS,
    SINGLE_SELECT,
    BoardSnapshot,
    ExistingField,
    field_spec,
    mismatched_fields,
    missing_fields,
    missing_labels,
)
from dispatchkit.github import APPLIED_FIELDS, DISPATCHKIT_LABEL
from dispatchkit.model import Lane, Verify
from dispatchkit.resolve import FIELD_ATTEMPTS, FIELD_STATUS, Status

pytestmark = pytest.mark.unit


def board(*fields: ExistingField, labels: tuple[str, ...] = (), items: int = 0) -> BoardSnapshot:
    return BoardSnapshot(fields=fields, labels=labels, items=items)


def complete_board(items: int = 0) -> BoardSnapshot:
    return BoardSnapshot(
        fields=tuple(
            ExistingField(name=spec.name, id=f"PVTF_{spec.name}", options=spec.options)
            for spec in REQUIRED_FIELDS
        ),
        labels=REQUIRED_LABELS,
        items=items,
    )


class TestContract:
    def test_every_status_the_scheduler_can_write_is_an_option_on_the_board(self) -> None:
        # The failure this prevents: a pass resolving a task to `Auto-merging`
        # and dying on the write because nobody added the option.
        assert set(field_spec(FIELD_STATUS).options) == {status.value for status in Status}

    def test_lane_and_verify_options_are_exactly_their_enums(self) -> None:
        assert set(field_spec("Lane").options) == {lane.value for lane in Lane}
        assert set(field_spec("Verify").options) == {verify.value for verify in Verify}

    def test_every_field_apply_writes_is_required(self) -> None:
        names = {spec.name for spec in REQUIRED_FIELDS}
        assert set(APPLIED_FIELDS) <= names

    def test_the_scheduler_owned_fields_are_required_too(self) -> None:
        names = {spec.name for spec in REQUIRED_FIELDS}
        assert {FIELD_STATUS, FIELD_ATTEMPTS} <= names

    def test_the_labels_the_query_filters_on_are_required(self) -> None:
        # `fetch_state` filters on this label, so a repository without it
        # returns an empty board and a pass that does nothing, silently.
        assert DISPATCHKIT_LABEL in REQUIRED_LABELS

    def test_a_lane_and_verify_label_exists_for_every_enum_value(self) -> None:
        for lane in Lane:
            assert f"lane:{lane.value}" in REQUIRED_LABELS
        for verify in Verify:
            assert f"verify:{verify.value}" in REQUIRED_LABELS


class TestMissing:
    def test_an_empty_board_is_missing_everything(self) -> None:
        assert missing_fields(board()) == REQUIRED_FIELDS
        assert missing_labels(board()) == REQUIRED_LABELS

    def test_a_complete_board_is_missing_nothing(self) -> None:
        snapshot = complete_board()
        assert missing_fields(snapshot) == ()
        assert missing_labels(snapshot) == ()
        assert mismatched_fields(snapshot) == ()

    def test_extra_fields_and_labels_are_left_alone(self) -> None:
        # The board is somebody else's too; dispatchkit owns its fields, not
        # the project.
        snapshot = complete_board()
        extended = BoardSnapshot(
            fields=(*snapshot.fields, ExistingField("Priority", "PVTF_x", ("P1",))),
            labels=(*snapshot.labels, "bug"),
            items=0,
        )
        assert missing_fields(extended) == ()
        assert missing_labels(extended) == ()
        assert mismatched_fields(extended) == ()


class TestMismatch:
    def test_the_built_in_status_field_is_a_mismatch_not_a_match(self) -> None:
        # A new project ships `Status` with Todo/In Progress/Done. It has the
        # right name and the wrong options, which is the case that used to
        # fail deep inside a pass.
        snapshot = complete_board()
        replaced = BoardSnapshot(
            fields=tuple(
                ExistingField(f.name, f.id, ("Todo", "In Progress", "Done"))
                if f.name == FIELD_STATUS
                else f
                for f in snapshot.fields
            ),
            labels=snapshot.labels,
            items=0,
        )
        ((spec, existing),) = mismatched_fields(replaced)
        assert spec.name == FIELD_STATUS
        assert "Todo" in existing.options

    def test_a_single_select_missing_one_option_is_a_mismatch(self) -> None:
        snapshot = complete_board()
        short = BoardSnapshot(
            fields=tuple(
                ExistingField(f.name, f.id, f.options[:-1]) if f.name == "Lane" else f
                for f in snapshot.fields
            ),
            labels=snapshot.labels,
            items=0,
        )
        assert [spec.name for spec, _ in mismatched_fields(short)] == ["Lane"]

    def test_extra_options_are_tolerated(self) -> None:
        # Someone else's option on our field costs nothing; we only ever write
        # values we own.
        snapshot = complete_board()
        wide = BoardSnapshot(
            fields=tuple(
                ExistingField(f.name, f.id, (*f.options, "Parked")) if f.name == FIELD_STATUS else f
                for f in snapshot.fields
            ),
            labels=snapshot.labels,
            items=0,
        )
        assert mismatched_fields(wide) == ()

    def test_a_text_field_that_is_really_a_single_select_is_a_mismatch(self) -> None:
        # The only kind check the API's field list actually supports: a field
        # with options where we need free text, or the reverse.
        snapshot = complete_board()
        wrong = BoardSnapshot(
            fields=tuple(
                ExistingField(f.name, f.id, ("a", "b")) if f.name == "Task ID" else f
                for f in snapshot.fields
            ),
            labels=snapshot.labels,
            items=0,
        )
        assert [spec.name for spec, _ in mismatched_fields(wrong)] == ["Task ID"]

    def test_a_single_select_with_no_options_at_all_is_a_mismatch(self) -> None:
        snapshot = complete_board()
        wrong = BoardSnapshot(
            fields=tuple(
                ExistingField(f.name, f.id, ()) if f.name == "Lane" else f for f in snapshot.fields
            ),
            labels=snapshot.labels,
            items=0,
        )
        assert [spec.name for spec, _ in mismatched_fields(wrong)] == ["Lane"]

    def test_the_single_select_kind_is_spelled_the_way_the_api_wants_it(self) -> None:
        assert field_spec(FIELD_STATUS).data_type == SINGLE_SELECT
        assert SINGLE_SELECT == "SINGLE_SELECT"
