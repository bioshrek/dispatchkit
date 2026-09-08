"""D5.5: `dispatchkit doctor` — is this repository able to run a pass?

Everything the pipeline needs but does not create for itself is a way for a
first run to fail confusingly, usually deep inside a pass and after a partial
write. The failures are always the same five:

| Check          | What goes wrong without it                                  |
| -------------- | ----------------------------------------------------------- |
| `token-scopes` | Every board write fails; issues still get assigned          |
| `coding-agent` | Cloud tasks are dispatched to nobody                        |
| `board-fields` | A status write fails on an option the board does not have   |
| `labels`       | The state query matches nothing, so a pass is a silent no-op |
| `workflow`     | Nothing ever runs unattended                                |

The verdict is a pure function of a snapshot, which is what makes the whole
check set testable offline — and makes the same function usable as the `live`
tier's assertion set, where the snapshot comes from a real repository.

Nothing here mutates anything. `doctor` says what is wrong; `init` fixes it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dispatchkit.board import (
    BoardSnapshot,
    ExistingField,
    mismatched_fields,
    missing_fields,
    missing_labels,
)

#: Scopes a human must grant. `project` is the one that is never there by
#: default and without which nothing board-related works at all.
REQUIRED_SCOPES = ("repo", "project")

_SCOPES = re.compile(r"Token scopes:\s*(?P<scopes>.*)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """What the remote side reports. `scopes` is `None` when unknowable."""

    scopes: tuple[str, ...] | None
    agent_available: bool
    board: BoardSnapshot


@dataclass(frozen=True, slots=True)
class LocalFacts:
    """What the working tree provides. Gathered by the CLI, never in here."""

    config_path: Path
    config_exists: bool
    workflow_path: Path
    workflow_exists: bool
    plans: Path
    plans_exists: bool


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    remedy: str = ""


def check(diagnostics: Diagnostics, facts: LocalFacts) -> tuple[Check, ...]:
    """Every check, in the order a first-time adopter hits them."""
    return (*check_remote(diagnostics), *check_local(facts))


def check_remote(diagnostics: Diagnostics) -> tuple[Check, ...]:
    """The checks that need a repository and a board to answer."""
    return (
        _scopes(diagnostics.scopes),
        _agent(diagnostics.agent_available),
        _fields(diagnostics.board),
        _labels(diagnostics.board),
    )


def check_local(facts: LocalFacts) -> tuple[Check, ...]:
    """The checks the working tree can answer on its own, offline."""
    return (_config(facts), _plans(facts), _workflow(facts))


def healthy(checks: Sequence[Check]) -> bool:
    return all(item.ok for item in checks)


def summarise(checks: Sequence[Check]) -> list[str]:
    """One line per check, plus a remedy line for each failure."""
    lines: list[str] = []
    for item in checks:
        lines.append(f"{'ok  ' if item.ok else 'FAIL'} {item.name}: {item.detail}")
        if not item.ok and item.remedy:
            lines.append(f"       -> {item.remedy}")
    return lines


def _scopes(scopes: tuple[str, ...] | None) -> Check:
    if scopes is None:
        # A workflow token has no scope line at all. Claiming this passed
        # would be a lie; failing it would be a false alarm on every CI run.
        return Check(
            "token-scopes",
            True,
            "could not determine token scopes (a workflow token does not report them)",
        )
    missing = [scope for scope in REQUIRED_SCOPES if scope not in scopes]
    if missing:
        return Check(
            "token-scopes",
            False,
            f"token is missing {_quoted(missing)} (it has: {_quoted(scopes) or 'nothing'})",
            f"gh auth refresh -s {','.join(missing)}",
        )
    return Check("token-scopes", True, f"token holds {_quoted(REQUIRED_SCOPES)}")


def _agent(available: bool) -> Check:
    if not available:
        return Check(
            "coding-agent",
            False,
            "the coding agent is not among this repository's assignable actors",
            "enable the GitHub coding agent, or route these tasks to the local lane "
            "with `lane = \"local\"`",
        )
    return Check("coding-agent", True, "the coding agent can be assigned")


def _fields(board: BoardSnapshot) -> Check:
    missing = missing_fields(board)
    mismatched = mismatched_fields(board)
    if not missing and not mismatched:
        return Check(
            "board-fields",
            True,
            f"the project has {_quoted(field.name for field in board.fields)}",
        )
    problems = [f"`{spec.name}` is missing" for spec in missing]
    problems += [
        f"`{spec.name}` cannot hold {_quoted(sorted(set(spec.options) - set(existing.options)))}"
        for spec, existing in mismatched
    ]
    return Check(
        "board-fields",
        False,
        "; ".join(problems),
        "dispatchkit init --repo owner/name --project N",
    )


def _labels(board: BoardSnapshot) -> Check:
    missing = missing_labels(board)
    if missing:
        return Check(
            "labels",
            False,
            f"the repository is missing {_quoted(missing)}",
            "dispatchkit init --repo owner/name --project N",
        )
    return Check("labels", True, "every label the pipeline filters on exists")


def _config(facts: LocalFacts) -> Check:
    if facts.config_exists:
        return Check("config", True, f"read from {facts.config_path}")
    # Every setting has a default, so no config is a legitimate choice.
    return Check(
        "config",
        True,
        f"no config file; defaults apply. {facts.config_path} is where one would be read from",
    )


def _plans(facts: LocalFacts) -> Check:
    if facts.plans_exists:
        return Check("plans", True, f"task graphs are read from {facts.plans}/")
    return Check(
        "plans",
        False,
        f"the plans directory {facts.plans}/ does not exist, so no graph can be applied",
        f"mkdir -p {facts.plans}, or set `paths.plans` in {facts.config_path}",
    )


def _workflow(facts: LocalFacts) -> Check:
    if facts.workflow_exists:
        return Check("workflow", True, f"the scheduler runs from {facts.workflow_path}")
    return Check(
        "workflow",
        False,
        f"{facts.workflow_path} does not exist, so no pass ever runs unattended",
        "dispatchkit init --repo owner/name --project N",
    )


def parse_token_scopes(text: str) -> tuple[str, ...] | None:
    """Read `gh auth status` output. `None` means the token does not say."""
    match = _SCOPES.search(text)
    if match is None:
        return None
    raw = match.group("scopes").strip()
    if not raw or raw == "none":
        return ()
    return tuple(scope.strip().strip("'\"") for scope in raw.split(",") if scope.strip())


def parse_labels(payload: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(str(entry["name"]) for entry in payload)


def parse_project(
    fields: dict[str, Any], *, items: int, labels: Sequence[str]
) -> BoardSnapshot:
    """`gh project field-list` plus the item count and the repository's labels."""
    return BoardSnapshot(
        fields=tuple(
            ExistingField(
                name=str(entry["name"]),
                id=str(entry["id"]),
                options=tuple(str(option["name"]) for option in entry.get("options") or ()),
            )
            for entry in fields.get("fields", [])
        ),
        labels=tuple(labels),
        items=items,
    )


def _quoted(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)
