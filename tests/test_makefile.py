"""The Makefile invokes commands that exist.

`make doctor REPO=owner/name` — the documented way to run the health check
against a repository — passed `--project`, which D14 deleted when it retired
the board. `make tick` called a subcommand D13 replaced with `watch --once`.
Both had been broken since those deliverables landed, and nothing noticed,
because `make check` runs the four gates and never the convenience targets.

Same failure as the schema page one deliverable back: a second place that
states what the CLI accepts, drifting silently from the CLI. And the same
remedy — pin it, rather than resolving to be more careful.

Deliberately not a full `make -n` expansion. That would reimplement enough of
make's syntax to have its own bugs, and the two defects here were both a name
the parser has never heard of, which a name check catches.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dispatchkit.cli import build_parser

pytestmark = pytest.mark.unit

MAKEFILE = Path(__file__).resolve().parent.parent / "Makefile"

#: `$(if $(REPO),--repo $(REPO),)` -- the only conditional the Makefile uses.
_CONDITIONAL = re.compile(r"\$\(if\s+\$\([A-Z]+\),([^,]*),[^)]*\)")
#: Any remaining `$(VAR)`.
_VARIABLE = re.compile(r"\$\(([A-Z]+)\)")


def recipes() -> list[str]:
    """Every indented recipe line that runs the CLI."""
    return [
        line.strip()
        for line in MAKEFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("\t") and "dispatchkit " in line
    ]


def argv_of(recipe: str) -> list[str]:
    """The recipe as the CLI would receive it, with make's variables filled in.

    Values are dummies: this asks whether the *shape* of the invocation is one
    the parser accepts, and both defects it was written for -- a subcommand and
    a flag that no longer exist -- are shape.
    """
    expanded = _CONDITIONAL.sub(lambda match: match.group(1), recipe)
    expanded = _VARIABLE.sub("placeholder", expanded)
    words = expanded.split()
    return words[words.index("dispatchkit") + 1 :]


class TestTheRecipesAreReal:
    def test_there_are_some(self) -> None:
        # Guards the extraction itself: a test that silently matches nothing
        # passes forever.
        assert len(recipes()) >= 4

    def test_the_parser_accepts_every_one(self) -> None:
        # `parse_args` exits 2 on an unknown subcommand or an unknown flag,
        # which is exactly the pair of defects this was written for. Nothing
        # is executed.
        for recipe in recipes():
            try:
                build_parser().parse_args(argv_of(recipe))
            except SystemExit as exit_code:  # pragma: no cover - the failure path
                pytest.fail(f"`{recipe}` is not a command: exit {exit_code.code}")

    def test_it_would_notice_a_dead_invocation(self) -> None:
        # The test's own smoke alarm.
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv_of("\tuv run dispatchkit tick --plan $(PLAN)"))

    def test_it_would_notice_a_dead_flag(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv_of("\tuv run dispatchkit doctor --project $(PROJECT)"))


class TestTheDeletedThingsAreGone:
    def test_the_board_left_no_flag_behind(self) -> None:
        # D14 retired the Project board. `--project` outliving it is what made
        # `make doctor REPO=...` exit 2 for two deliverables.
        assert "--project" not in MAKEFILE.read_text(encoding="utf-8")
