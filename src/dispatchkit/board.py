"""The board contract (D5.5).

`Status` is a projection of the issues, but the Project board is typed: a
single-select field rejects a value that is not one of its options. So the
scheduler can only write statuses the board was built to hold, and "was it
built correctly" is a question `doctor` must be able to answer before a pass
runs rather than halfway through one.

Everything the board must provide is declared once, here, derived from the
enums that generate the values — `Status`, `Lane`, `Verify` — so a new status
cannot be added without the board growing an option for it.

Only two things about an existing field are actually observable through
`gh project field-list`: its name and, for a single select, its options. There
is no way to tell a text field from a number field, so this contract checks
what can be checked — presence, and *option-ness* — and says nothing it cannot
prove.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from dispatchkit.github import DISPATCHKIT_LABEL, FIELD_LANE, FIELD_TASK_ID, FIELD_VERIFY
from dispatchkit.model import Lane, Verify
from dispatchkit.resolve import FIELD_ATTEMPTS, FIELD_STATUS, Status

#: `--data-type` values `gh project field-create` accepts.
SINGLE_SELECT = "SINGLE_SELECT"
TEXT = "TEXT"
NUMBER = "NUMBER"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """A field dispatchkit needs, and the options it must offer."""

    name: str
    data_type: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExistingField:
    """A field the project already has, as the API reports it."""

    name: str
    id: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoardSnapshot:
    """What a project and repository currently provide.

    `items` is carried because it decides whether a wrong field may be
    recreated: dropping a field drops its values, which is free on an empty
    board and destructive on a populated one.
    """

    fields: tuple[ExistingField, ...] = ()
    labels: tuple[str, ...] = ()
    items: int = 0

    def field(self, name: str) -> ExistingField | None:
        return next((field for field in self.fields if field.name == name), None)


REQUIRED_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(FIELD_STATUS, SINGLE_SELECT, tuple(status.value for status in Status)),
    FieldSpec(FIELD_LANE, SINGLE_SELECT, tuple(lane.value for lane in Lane)),
    FieldSpec(FIELD_VERIFY, SINGLE_SELECT, tuple(verify.value for verify in Verify)),
    FieldSpec(FIELD_TASK_ID, TEXT),
    FieldSpec(FIELD_ATTEMPTS, NUMBER),
)

#: Labels the pipeline filters on. `dispatchkit` is the important one: the
#: state query selects by it, so a repository without it returns an empty
#: board and a pass that silently does nothing.
REQUIRED_LABELS: tuple[str, ...] = (
    DISPATCHKIT_LABEL,
    *(f"lane:{lane.value}" for lane in Lane),
    *(f"verify:{verify.value}" for verify in Verify),
)


def field_spec(name: str) -> FieldSpec:
    return next(spec for spec in REQUIRED_FIELDS if spec.name == name)


def missing_fields(snapshot: BoardSnapshot) -> tuple[FieldSpec, ...]:
    return tuple(spec for spec in REQUIRED_FIELDS if snapshot.field(spec.name) is None)


def missing_labels(snapshot: BoardSnapshot) -> tuple[str, ...]:
    return tuple(label for label in REQUIRED_LABELS if label not in snapshot.labels)


def mismatched_fields(
    snapshot: BoardSnapshot,
) -> tuple[tuple[FieldSpec, ExistingField], ...]:
    """Fields that exist under the right name but cannot hold our values.

    Extra options are fine — we only ever write values we own — so this is a
    subset test, not an equality test.
    """
    mismatches: list[tuple[FieldSpec, ExistingField]] = []
    for spec in REQUIRED_FIELDS:
        existing = snapshot.field(spec.name)
        if existing is None:
            continue
        if spec.data_type == SINGLE_SELECT:
            if not set(spec.options) <= set(existing.options):
                mismatches.append((spec, existing))
        elif existing.options:
            mismatches.append((spec, existing))
    return tuple(mismatches)


class BoardApi(Protocol):
    """The port `init` needs. The adapter is `gh_cli.GhCli`.

    Separate from `GitHubApi` on purpose: setting a board up and running a
    scheduler pass are different jobs with different blast radii, and the pass
    should not be handed a client that can delete a field.
    """

    def fetch_board(self) -> BoardSnapshot: ...

    def create_field(self, spec: FieldSpec) -> None: ...

    def delete_field(self, *, field_id: str) -> None: ...

    def ensure_labels(self, labels: Sequence[str]) -> None: ...
