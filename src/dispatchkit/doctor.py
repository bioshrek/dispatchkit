"""D5.5: `dispatchkit doctor` — is this repository able to run a pass?

Everything the pipeline needs but does not create for itself is a way for a
first run to fail confusingly, usually deep inside a pass and after a partial
write. The failures are always the same four:

| Check          | What goes wrong without it                                   |
| -------------- | ------------------------------------------------------------ |
| `token-scopes` | Nothing can be read or written at all                        |
| `coding-agent` | Cloud tasks are dispatched to nobody                         |
| `labels`       | The state query matches nothing, so a pass is a silent no-op |
| `plans`        | There is no graph to apply                                   |

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

from dispatchkit.github import missing_labels

#: Scopes a human must grant. Just the one since D14: issues, labels, pull
#: requests and the assignment mutation are all `repo`, and there is no board
#: to need `project` for.
REQUIRED_SCOPES = ("repo",)

_SCOPES = re.compile(r"Token scopes:\s*(?P<scopes>.*)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """What the remote side reports. `scopes` is `None` when the token is silent."""

    scopes: tuple[str, ...] | None
    agent_available: bool
    #: Every label the repository defines. The state query filters on
    #: `dispatchkit`, so a repository missing it answers nothing at all.
    labels: tuple[str, ...] = ()
    #: Is the default branch protected by required status checks? Not a
    #: precondition for merging — dispatchkit checks CI itself — but it decides
    #: whether anything is watching if that reading is wrong.
    protected_branch: bool = False


@dataclass(frozen=True, slots=True)
class LocalFacts:
    """What the working tree provides. Gathered by the CLI, never in here."""

    config_path: Path
    config_exists: bool
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
    """The checks that need a repository, and so a credential, to answer."""
    return (
        _scopes(diagnostics.scopes),
        _agent(diagnostics.agent_available),
        _labels(diagnostics.labels),
        _merge_gate(diagnostics),
    )


def check_local(facts: LocalFacts) -> tuple[Check, ...]:
    """The checks the working tree can answer on its own, offline."""
    return (_config(facts), _plans(facts))


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
        # This branch used to pass, because a workflow token reports no scope
        # line and failing every CI run would have been a false alarm. Since
        # D13 there is no unattended pass, so the only case left is the one
        # that actually happens — not logged in at all. It says what it saw.
        return Check(
            "token-scopes",
            False,
            "`gh auth status` reported no token scopes, so there is probably no "
            "credential here at all",
            "gh auth login",
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
            'with `lane = "local"`',
        )
    return Check("coding-agent", True, "the coding agent can be assigned")


def _labels(labels: Sequence[str]) -> Check:
    missing = missing_labels(labels)
    if missing:
        return Check(
            "labels",
            False,
            f"the repository is missing {_quoted(missing)}",
            "dispatchkit init --repo owner/name",
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


def _quoted(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)
