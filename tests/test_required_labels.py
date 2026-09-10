"""D6.6: `init` creates every label the pipeline writes, not just the ones it reads.

Found by running the local lane against a real repository for the first time.
It died on its first action: marking issue #13 with `dispatch:local`, a label
`init` had never created, so `gh issue edit --add-label` failed and the pass
tracebacked out.

`REQUIRED_LABELS` carried a docstring calling itself "the only thing `init` has
to create on the repository itself", and it listed only the labels the *state
query filters on* — `dispatchkit`, the lanes, the verifies. Every label the
scheduler **writes** was missing: `dispatch:local`, `dispatch:stuck`,
`dispatch:hold`.

The cloud lane never noticed because a cloud dispatch is an assignment rather
than a label, and no live task had ever exhausted its retry budget. So the two
paths that would have caught this are exactly the two that had never run.

The scan below is the part that matters. Naming the three labels fixes today's
bug; requiring *every* `dispatch:`-prefixed constant in the package to be
creatable is what catches the fourth one, whenever somebody adds it — and the
labels are spread across `github.py` and `resolve.py`, which is how these three
came to be overlooked in the first place.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import dispatchkit
from dispatchkit.doctor import LocalFacts
from dispatchkit.github import (
    DISPATCHKIT_LABEL,
    LABEL_HOLD,
    LABEL_LOCAL_CLAIM,
    LABEL_STUCK,
    REQUIRED_LABELS,
    missing_labels,
)
from dispatchkit.init import CreateLabel, plan_init

pytestmark = pytest.mark.unit


def declared_labels() -> dict[str, str]:
    """Every `LABEL_*` constant in the package, as name -> value."""
    found: dict[str, str] = {}
    for module in pkgutil.iter_modules(dispatchkit.__path__):
        imported = importlib.import_module(f"dispatchkit.{module.name}")
        for name in dir(imported):
            if name.startswith("LABEL_"):
                value = getattr(imported, name)
                if isinstance(value, str):
                    found[name] = value
    return found


class TestTheScanItself:
    """A scan that silently matches nothing passes for ever."""

    def test_it_finds_the_labels(self) -> None:
        found = declared_labels()
        assert len(found) >= 3, found

    def test_it_reaches_more_than_one_module(self) -> None:
        # D6.6 gathered the `dispatch:` labels into `github.py`, so a scan of
        # that one module would pass today — and would go blind the moment
        # somebody puts the next label where `LABEL_STUCK` used to live.
        values = set(declared_labels().values())
        assert {LABEL_STUCK, LABEL_HOLD} <= values


class TestEveryWrittenLabelIsCreatable:
    def test_the_local_claim(self) -> None:
        # The one that actually broke: without it the local lane cannot take
        # its first action in any repository `init` set up.
        assert LABEL_LOCAL_CLAIM in REQUIRED_LABELS

    def test_the_stuck_label(self) -> None:
        # D7 writes this when a task exhausts its retry budget — the moment a
        # human most needs the pipeline to still be working.
        assert LABEL_STUCK in REQUIRED_LABELS

    def test_the_hold_label(self) -> None:
        # Read rather than written, but a human cannot apply a label that the
        # repository does not offer, and applying it is the entire feature.
        assert LABEL_HOLD in REQUIRED_LABELS

    def test_every_dispatch_label_in_the_package(self) -> None:
        uncreatable = {
            name: value
            for name, value in declared_labels().items()
            if value.startswith("dispatch:") and value not in REQUIRED_LABELS
        }
        assert not uncreatable, (
            f"the pipeline uses these but `init` never creates them: {uncreatable}"
        )

    def test_the_filter_label_is_still_there(self) -> None:
        # The state query selects by it; without it a pass is a silent no-op.
        assert DISPATCHKIT_LABEL in REQUIRED_LABELS


class TestInitActuallyPlansThem:
    """The constant is only half of it; `init` has to act on it."""

    def test_a_bare_repository_gets_all_of_them(self, tmp_path: Path) -> None:
        plan = plan_init((), _facts(tmp_path))
        planned = {op.name for op in plan.operations if isinstance(op, CreateLabel)}
        assert set(REQUIRED_LABELS) <= planned

    def test_the_local_lane_could_now_take_its_mark(self, tmp_path: Path) -> None:
        plan = plan_init((), _facts(tmp_path))
        planned = {op.name for op in plan.operations if isinstance(op, CreateLabel)}
        assert LABEL_LOCAL_CLAIM in planned

    def test_nothing_is_replanned_once_they_exist(self, tmp_path: Path) -> None:
        assert missing_labels(REQUIRED_LABELS) == ()


def _facts(root: Path) -> LocalFacts:
    return LocalFacts(
        config_path=root / ".github" / "dispatchkit.toml",
        config_exists=False,
        plans=root / "docs" / "plans",
        plans_exists=False,
    )
