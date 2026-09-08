"""D5.5: `dispatchkit init` — make a repository able to run a pass.

`doctor` names what is missing; `init` creates it. The same idempotency
standard as `apply` applies, and for the same reason: this is run against a
board somebody may already be using, possibly twice, possibly concurrently
with a scheduler pass. So the plan is data, the second plan against the
result must be empty, and nothing that already exists is ever overwritten.

The interesting case is GitHub's built-in `Status` field, which arrives with
`Todo`/`In Progress`/`Done` — the right name and the wrong options. Fixing it
means deleting the field, which deletes its values. That is free on an empty
board and destructive on a populated one, so the item count decides, and a
populated board gets a notice instead of an operation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from dispatchkit.board import (
    REQUIRED_FIELDS,
    REQUIRED_LABELS,
    BoardSnapshot,
    ExistingField,
    FieldSpec,
)
from dispatchkit.config import load_config
from dispatchkit.doctor import Diagnostics, LocalFacts, check, healthy
from dispatchkit.init import (
    CONFIG_TEMPLATE,
    WORKFLOW_TEMPLATE,
    CreateField,
    CreateLabel,
    MakeDirectory,
    RecreateField,
    WriteFile,
    execute_init,
    plan_init,
)
from dispatchkit.model import Lane
from dispatchkit.resolve import FIELD_STATUS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeBoard:
    """In-memory project + repository, so the convergence test can re-read."""

    snapshot: BoardSnapshot = field(default_factory=BoardSnapshot)
    calls: list[str] = field(default_factory=list)

    def fetch_board(self) -> BoardSnapshot:
        self.calls.append("fetch_board()")
        return self.snapshot

    def create_field(self, spec: FieldSpec) -> None:
        self.calls.append(f"create_field({spec.name})")
        assert self.snapshot.field(spec.name) is None, f"{spec.name} already exists"
        self.snapshot = replace(
            self.snapshot,
            fields=(
                *self.snapshot.fields,
                ExistingField(spec.name, f"PVTF_{spec.name}", spec.options),
            ),
        )

    def delete_field(self, *, field_id: str) -> None:
        self.calls.append(f"delete_field({field_id})")
        self.snapshot = replace(
            self.snapshot,
            fields=tuple(f for f in self.snapshot.fields if f.id != field_id),
        )

    def ensure_labels(self, labels: Sequence[str]) -> None:
        self.calls.append(f"ensure_labels({sorted(labels)})")
        fresh = tuple(name for name in labels if name not in self.snapshot.labels)
        self.snapshot = replace(self.snapshot, labels=(*self.snapshot.labels, *fresh))


def facts(root: Path) -> LocalFacts:
    config = root / ".github" / "dispatchkit.toml"
    workflow = root / ".github" / "workflows" / "dispatchkit.yml"
    plans = root / "docs" / "plans"
    return LocalFacts(
        config_path=config,
        config_exists=config.exists(),
        workflow_path=workflow,
        workflow_exists=workflow.exists(),
        plans=plans,
        plans_exists=plans.exists(),
    )


def complete_board() -> BoardSnapshot:
    return BoardSnapshot(
        fields=tuple(
            ExistingField(spec.name, f"PVTF_{spec.name}", spec.options)
            for spec in REQUIRED_FIELDS
        ),
        labels=REQUIRED_LABELS,
    )


class TestPlanning:
    def test_an_empty_board_gets_every_field_and_label(self, tmp_path: Path) -> None:
        plan = plan_init(BoardSnapshot(), facts(tmp_path))
        created = [op.spec.name for op in plan.operations if isinstance(op, CreateField)]
        labels = [op.name for op in plan.operations if isinstance(op, CreateLabel)]

        assert created == [spec.name for spec in REQUIRED_FIELDS]
        assert labels == list(REQUIRED_LABELS)

    def test_a_complete_board_in_a_complete_tree_needs_nothing(self) -> None:
        assert plan_init(complete_board(), facts(ROOT)).operations == ()

    def test_a_bare_tree_gets_a_config_a_workflow_and_a_plans_directory(
        self, tmp_path: Path
    ) -> None:
        plan = plan_init(complete_board(), facts(tmp_path))
        written = {op.path.name for op in plan.operations if isinstance(op, WriteFile)}
        made = {op.path.name for op in plan.operations if isinstance(op, MakeDirectory)}

        assert written == {"dispatchkit.toml", "dispatchkit.yml"}
        assert made == {"plans"}

    def test_an_existing_file_is_never_overwritten(self, tmp_path: Path) -> None:
        # Someone else's config is not ours to rewrite, and this command is
        # explicitly safe to re-run.
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "dispatchkit.toml").write_text("[caps]\ncloud = 9\n")
        plan = plan_init(complete_board(), facts(tmp_path))
        assert not any(
            isinstance(op, WriteFile) and op.path.name == "dispatchkit.toml"
            for op in plan.operations
        )


class TestTheBuiltInStatusField:
    def built_in(self, items: int = 0) -> BoardSnapshot:
        board = complete_board()
        return replace(
            board,
            fields=tuple(
                ExistingField(f.name, f.id, ("Todo", "In Progress", "Done"))
                if f.name == FIELD_STATUS
                else f
                for f in board.fields
            ),
            items=items,
        )

    def test_on_an_empty_board_it_is_replaced(self, tmp_path: Path) -> None:
        plan = plan_init(self.built_in(), facts(tmp_path))
        (recreate,) = [op for op in plan.operations if isinstance(op, RecreateField)]
        assert recreate.spec.name == FIELD_STATUS
        assert recreate.field_id == f"PVTF_{FIELD_STATUS}"

    def test_on_a_populated_board_it_is_reported_and_left_alone(self, tmp_path: Path) -> None:
        # Deleting a field deletes its values. With items on the board that is
        # somebody's data, so it is a human's call, not a machine's.
        plan = plan_init(self.built_in(items=7), facts(tmp_path))
        assert not any(isinstance(op, RecreateField) for op in plan.operations)
        (notice,) = [n for n in plan.notices if n.code == "field-options"]
        assert FIELD_STATUS in notice.message
        assert "Auto-merging" in notice.message

    def test_the_notice_names_the_command_that_would_destroy_the_values(
        self, tmp_path: Path
    ) -> None:
        plan = plan_init(self.built_in(items=7), facts(tmp_path))
        (notice,) = [n for n in plan.notices if n.code == "field-options"]
        assert "field-delete" in notice.message


class TestExecution:
    def test_it_creates_fields_labels_and_files(self, tmp_path: Path) -> None:
        api = FakeBoard()
        result = execute_init(plan_init(api.fetch_board(), facts(tmp_path)), api)

        assert result.fields == len(REQUIRED_FIELDS)
        assert result.labels == len(REQUIRED_LABELS)
        assert result.files == 2
        assert (tmp_path / ".github" / "workflows" / "dispatchkit.yml").exists()
        assert (tmp_path / "docs" / "plans").is_dir()

    def test_labels_are_created_in_one_call_not_one_call_each(self, tmp_path: Path) -> None:
        api = FakeBoard()
        execute_init(plan_init(api.fetch_board(), facts(tmp_path)), api)
        assert len([call for call in api.calls if call.startswith("ensure_labels")]) == 1

    def test_a_recreated_field_is_deleted_before_it_is_created(self, tmp_path: Path) -> None:
        # The other order would fail: two fields cannot share a name.
        board = complete_board()
        api = FakeBoard(
            replace(
                board,
                fields=tuple(
                    ExistingField(f.name, f.id, ("Todo",)) if f.name == FIELD_STATUS else f
                    for f in board.fields
                ),
            )
        )
        execute_init(plan_init(api.fetch_board(), facts(tmp_path)), api)
        calls = [c for c in api.calls if "field" in c and "fetch" not in c]
        assert calls == [f"delete_field(PVTF_{FIELD_STATUS})", f"create_field({FIELD_STATUS})"]


class TestIdempotency:
    def test_a_second_pass_over_the_result_plans_nothing(self, tmp_path: Path) -> None:
        # The standard for anything that mutates: apply, re-read, re-plan, and
        # the second plan must be empty.
        api = FakeBoard()
        execute_init(plan_init(api.fetch_board(), facts(tmp_path)), api)

        again = plan_init(api.fetch_board(), facts(tmp_path))
        assert again.operations == ()

    def test_running_it_twice_changes_nothing_the_first_run_wrote(self, tmp_path: Path) -> None:
        api = FakeBoard()
        execute_init(plan_init(api.fetch_board(), facts(tmp_path)), api)
        written = (tmp_path / ".github" / "dispatchkit.toml").read_text(encoding="utf-8")

        execute_init(plan_init(api.fetch_board(), facts(tmp_path)), api)
        assert (tmp_path / ".github" / "dispatchkit.toml").read_text(encoding="utf-8") == written


class TestTheResultIsHealthy:
    def test_doctor_passes_on_a_repository_init_just_set_up(self, tmp_path: Path) -> None:
        """The two commands are one contract, so they are tested as one.

        `init` creating something `doctor` still complains about — or `doctor`
        demanding something `init` never creates — is the failure mode that
        makes an on-ramp worse than no on-ramp.
        """
        api = FakeBoard()
        execute_init(plan_init(api.fetch_board(), facts(tmp_path)), api)

        checks = check(
            Diagnostics(scopes=("repo", "project"), agent_available=True, board=api.fetch_board()),
            facts(tmp_path),
        )
        assert healthy(checks), [c.detail for c in checks if not c.ok]


class TestTemplatesMatchThisRepository:
    """What `init` writes is what this repository runs — asserted, not hoped.

    `tests/test_workflow.py` asserts the permissions, triggers and script
    safety of the file on disk. Pinning the template to that file is what
    extends those assertions to every repository `init` ever touches.
    """

    def test_the_workflow_template_is_this_repositorys_workflow(self) -> None:
        on_disk = (ROOT / ".github" / "workflows" / "dispatchkit.yml").read_text(encoding="utf-8")
        assert WORKFLOW_TEMPLATE == on_disk

    def test_the_config_template_is_this_repositorys_config(self) -> None:
        on_disk = (ROOT / ".github" / "dispatchkit.toml").read_text(encoding="utf-8")
        assert CONFIG_TEMPLATE == on_disk

    def test_the_config_template_states_the_defaults_it_documents(self, tmp_path: Path) -> None:
        # A template that does not parse, or that quietly changes a cap, would
        # be found by an adopter rather than by us.
        path = tmp_path / "dispatchkit.toml"
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        config = load_config(path)

        assert (config.cap(Lane.CLOUD), config.cap(Lane.LOCAL)) == (3, 1)
        assert config.retry_budget == 3
        assert config.plans == Path("docs/plans")
        assert config.is_fenced(".github/workflows/dispatchkit.yml")
