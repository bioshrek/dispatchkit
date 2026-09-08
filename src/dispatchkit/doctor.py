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

#: Actions configuration the unattended pass reads. Without them the scheduler
#: runs on its cron and exits non-zero with empty arguments.
REQUIRED_VARIABLES = ("DISPATCHKIT_PLAN", "DISPATCHKIT_PROJECT")
REQUIRED_SECRETS = ("DISPATCHKIT_TOKEN",)

_SCOPES = re.compile(r"Token scopes:\s*(?P<scopes>.*)$", re.MULTILINE)
_PYTHONPATH = re.compile(r"^\s*PYTHONPATH:\s*(?P<path>\S+)\s*$", re.MULTILINE)
_CHECKOUT_PATH = re.compile(r"^\s*path:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """What the remote side reports. `scopes` is `None` when unknowable."""

    scopes: tuple[str, ...] | None
    agent_available: bool
    board: BoardSnapshot
    #: Actions variables and secret *names* configured on the repository. The
    #: unattended pass reads its plan, project and token from these, so an
    #: empty one is a scheduler that runs on a cron and does nothing.
    variables: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    #: Is the default branch protected by required status checks? Not a
    #: precondition for merging — dispatchkit checks CI itself — but it decides
    #: whether anything is watching if that reading is wrong.
    protected_branch: bool = False


@dataclass(frozen=True, slots=True)
class LocalFacts:
    """What the working tree provides. Gathered by the CLI, never in here."""

    config_path: Path
    config_exists: bool
    workflow_path: Path
    workflow_exists: bool
    plans: Path
    plans_exists: bool
    #: The workflow's text, so the source it puts on `PYTHONPATH` can be
    #: checked against what the repository actually has.
    workflow_text: str = ""
    #: Does this repository carry `src/dispatchkit` itself? True here, false
    #: in every repository `init` writes into.
    vendored: bool = False


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
        _inputs(diagnostics),
        _merge_gate(diagnostics),
    )


def check_local(facts: LocalFacts) -> tuple[Check, ...]:
    """The checks the working tree can answer on its own, offline."""
    return (_config(facts), _plans(facts), _workflow(facts), _workflow_source(facts))


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


def _workflow_source(facts: LocalFacts) -> Check:
    """Will the workflow be able to import dispatchkit when it runs?

    Existing is not enough. A workflow that puts a directory on `PYTHONPATH`
    which nothing ever creates fails on `No module named dispatchkit`, on a
    schedule, with nobody reading the log — so the two halves are checked
    against each other: whatever `PYTHONPATH` names must either be fetched by
    a checkout step in the same file, or already be in this repository.
    """
    if not facts.workflow_exists:
        # `workflow` already reports this, and pointing twice at one fix is
        # noise.
        return Check("workflow-source", True, "no workflow to check")

    match = _PYTHONPATH.search(facts.workflow_text)
    if match is None:
        return Check(
            "workflow-source",
            False,
            f"{facts.workflow_path} sets no PYTHONPATH, so the pass cannot import dispatchkit",
            "dispatchkit init --repo owner/name --project N",
        )

    source = match.group("path")
    fetched = set(_CHECKOUT_PATH.findall(facts.workflow_text))
    if any(source == path or source.startswith(f"{path}/") for path in fetched):
        return Check("workflow-source", True, f"the pass fetches dispatchkit into {source}")
    if facts.vendored and source.split("/")[0] == "src":
        return Check("workflow-source", True, "the pass runs this repository's own source")
    return Check(
        "workflow-source",
        False,
        f"{facts.workflow_path} puts `{source}` on PYTHONPATH, but nothing in this "
        "repository or in the workflow provides it, so every pass will fail with "
        "`No module named dispatchkit`",
        # Not "re-run init": it writes the workflow only when there is none,
        # because overwriting somebody's workflow uninvited is worse than
        # leaving a stale one. Saying so is the difference between a remedy
        # and a wild goose chase.
        f"delete {facts.workflow_path} and re-run `dispatchkit init`, which writes a "
        "workflow that fetches dispatchkit's source itself; `init` will not overwrite "
        "a workflow that already exists",
    )


def _inputs(diagnostics: Diagnostics) -> Check:
    """The variables and the secret the unattended pass reads."""
    missing = [name for name in REQUIRED_VARIABLES if name not in diagnostics.variables]
    missing += [name for name in REQUIRED_SECRETS if name not in diagnostics.secrets]
    if not missing:
        return Check(
            "workflow-inputs",
            True,
            f"the repository sets {_quoted(REQUIRED_VARIABLES)}, and "
            f"{_quoted(REQUIRED_SECRETS)} by name — GitHub never discloses a "
            "secret's value, so whether the token is accepted is only learnt "
            "from a pass",
        )
    remedy = [
        f"gh variable set {name}" for name in REQUIRED_VARIABLES if name in missing
    ]
    # Never `gh secret set NAME BODY`: a token on a command line lands in the
    # shell history. `gh` prompts for the value when it is not given one.
    remedy += [f"gh secret set {name}" for name in REQUIRED_SECRETS if name in missing]
    return Check(
        "workflow-inputs",
        False,
        f"the repository is missing {_quoted(missing)}, so an unattended pass "
        f"runs with {_consequence(missing)}",
        "; ".join(remedy),
    )


#: What the absence of each input costs, so the message says only what is true.
_CONSEQUENCES = {
    "DISPATCHKIT_PLAN": "no plan",
    "DISPATCHKIT_PROJECT": "no board",
    "DISPATCHKIT_TOKEN": "no token",
}


def _merge_gate(diagnostics: Diagnostics) -> Check:
    """Is there a second lock behind a `verify: auto` merge?

    Reported rather than enforced. `merge_ops` refuses to merge anything that
    is not green, non-draft and outside the fence, and that gate does not
    depend on the repository's settings — which is the whole point, because a
    live probe showed the repository is no backstop at all: with no protection,
    GitHub merged a pull request the instant it was asked, reporting success,
    having run nothing.

    So an unprotected branch is not broken, it is *single-gated*. Every belief
    dispatchkit holds about CI is then the only thing standing between an agent
    and `main`. Protection makes GitHub refuse a red merge independently, which
    is the difference between one lock and two.
    """
    if diagnostics.protected_branch:
        return Check(
            "merge-gate",
            True,
            "the default branch requires status checks, so a `verify: auto` "
            "merge is refused by GitHub as well as by dispatchkit",
        )
    return Check(
        "merge-gate",
        False,
        "the default branch has no required status checks, so dispatchkit's "
        "own reading of CI is the only gate on a `verify: auto` merge",
        remedy=(
            "add branch protection requiring the check your `acceptance` "
            "command runs, so a red pull request is refused independently"
        ),
    )


def _consequence(missing: Sequence[str]) -> str:
    parts = [_CONSEQUENCES[name] for name in missing if name in _CONSEQUENCES]
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


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
