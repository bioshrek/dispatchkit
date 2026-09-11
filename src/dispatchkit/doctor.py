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

from dispatchkit.config import DEFAULT_LOCATIONS, GRAPH_SUFFIX, SchedulerConfig
from dispatchkit.github import missing_labels
from dispatchkit.version import __version__, at_least

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
    #: The branches a `verify: auto` merge actually targets that have no
    #: required status checks (D16). Every plan integrates on its own base now,
    #: so `main` is no longer the branch to ask about — and asking about it
    #: anyway was wrong in the reassuring direction.
    unprotected_bases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LocalFacts:
    """What the working tree provides. Gathered by the CLI, never in here."""

    config_path: Path
    config_exists: bool
    plans: Path
    plans_exists: bool
    #: The loaded config, when there is one to check. `None` means the caller
    #: did not load it, and the checks that read it stay quiet rather than
    #: reporting a default they were never handed.
    config: SchedulerConfig | None = None
    #: `config_path` as the *repository* sees it, which is what a fence pattern
    #: is compared against. Differs from `config_path` whenever `--root` points
    #: somewhere other than the working directory.
    config_in_repo: Path | None = None


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
    checks = [
        check_version(facts.config.requires_version if facts.config else None),
        _config(facts),
        _plans(facts),
    ]
    if facts.config is not None:
        checks.append(check_fence(facts.config, facts.config_in_repo or facts.config_path))
    return tuple(checks)


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


def check_cli(program: str, *, found: str | None) -> Check:
    """Is `gh` on this machine at all? (D11)

    The single hard external dependency, and so the one failure that makes
    every other remote check unanswerable. Reported first and on its own,
    because the translated adapter error reads as "the repository could not be
    read", which sends somebody to look at a repository that is fine.

    Same shape as `check_runner`: this decides, and the CLI goes and looks.
    """
    if found is None:
        return Check(
            "gh",
            False,
            f"`{program}` is not installed or not on PATH, so nothing can be read "
            "from or written to GitHub",
            f"install it from https://cli.github.com, then `{program} auth login`",
        )
    return Check("gh", True, f"`{program}` is at {found}")


def check_runner(program: str, *, found: str | None) -> Check:
    """Is the local runner actually on this machine? (D6.5)

    Only `argv[0]` is in question. Everything after it is substitution, which
    `RunnerConfig` validates at load, so the one thing left that can be wrong
    here is whether the program exists — and finding that out when a task is
    already marked costs a dispatch and a comment on somebody's issue.

    The lookup itself is the CLI's, like every other fact here: this decides,
    and something else goes and looks.
    """
    if not program:
        return Check(
            "local-runner",
            False,
            "`runner.argv` names no program",
            "set runner.argv in .github/dispatchkit.toml",
        )
    if found is None:
        return Check(
            "local-runner",
            False,
            f"`{program}` is not on PATH, so `watch --local` could not run anything",
            f"install {program}, or drop --local and route these tasks to the cloud lane",
        )
    return Check("local-runner", True, f"`{program}` is at {found}")


def check_fence(config: SchedulerConfig, config_path: Path) -> Check:
    """Does the blast-radius fence cover the things that define the pipeline?

    `default_fence` is careful about this — it fences the workflows, the plans
    glob and *both* documented config locations, on the grounds that a config
    which moves house must not be able to arrive unreviewed. An explicit
    `fence.paths` replaces that list outright, and so throws all of it away.
    A safe default with an unguarded override.

    It matters more since D9.2, which demoted `touches` and left the fence as
    the only permission boundary in the system. The escalation is short: a
    `verify: auto` pull request editing the config is unfenced, so `merge_ops`
    merges it on green CI; the next pass loads it; and `runner.argv` is a
    command `watch --local` runs on the maintainer's machine. Fencing the
    workflows matters for the same reason one step back — they decide what
    green means — and the plans glob because the graph is the reviewed
    artifact, so rewriting it unattended is what makes the review a formality.

    Reported rather than enforced, like `merge-gate`: this says the pipeline
    can rewrite its own governance, and a human decides what to do about it.
    """
    if not config.fence:
        return Check(
            "fence",
            False,
            "the blast-radius fence is empty, so nothing is withheld from an "
            "unattended merge — including the config, the workflows and the plans",
            _FENCE_REMEDY,
        )

    unprotected = [
        f"`{path}`" for path in _must_be_fenced(config, config_path) if not config.is_fenced(path)
    ]
    if unprotected:
        return Check(
            "fence",
            False,
            f"the fence does not cover {', '.join(unprotected)}, so a `verify: auto` "
            "pull request may rewrite what governs the pipeline and merge it unattended",
            _FENCE_REMEDY,
        )
    return Check("fence", True, "the fence covers the config, the workflows and the plans")


_FENCE_REMEDY = (
    "add the missing patterns to `fence.paths`, or drop the key entirely to get "
    "the derived default, which already covers all three"
)


def _must_be_fenced(config: SchedulerConfig, config_path: Path) -> list[str]:
    """Representative repository paths the fence has to withhold.

    Paths rather than patterns, because the question is whether a file *would*
    be withheld, not whether the fence is spelled a particular way —
    `.github/**` is a perfectly good way to cover the first two.

    A config location that takes precedence over the one in use is included:
    discovery prefers `.github/dispatchkit.toml`, so with the root file in use
    an agent that merely *creates* the `.github` one silently takes over.
    """
    required = [".github/workflows/ci.yml", f"{_posix(config.plans)}/example{GRAPH_SUFFIX}"]
    # An absolute path is not a repository path, so no pattern could cover it.
    if not config_path.is_absolute():
        required.append(_posix(config_path))
    for location in DEFAULT_LOCATIONS:
        if _posix(location) == _posix(config_path):
            break
        required.append(_posix(location))
    return required


def _posix(path: Path) -> str:
    return path.as_posix()


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


def check_version(floor: str | None) -> Check:
    """What is running, and whether the repository agrees it is new enough.

    Offline and credential-free on purpose: it is most useful to an adopter
    who has not authenticated yet, and an operator asking what they are
    running is the question every other answer depends on.

    An unreadable pin fails. `at_least` returns `None` rather than guessing,
    and passing silently here would be the reassuring wrong answer -- a pin
    nobody can parse is a pin nobody is protected by.
    """
    if floor is None:
        return Check("version", True, f"dispatchkit {__version__}")

    satisfied = at_least(__version__, floor)
    if satisfied:
        return Check("version", True, f"dispatchkit {__version__}, and this repo wants >= {floor}")
    detail = (
        f"this repo wants dispatchkit >= {floor} and this is {__version__}"
        if satisfied is False
        else f"this repo's `requires_version` is `{floor}`, which is not a version "
        f"(running {__version__})"
    )
    return Check(
        "version",
        False,
        detail,
        "uv tool install --force git+https://github.com/bioshrek/dispatchkit@v"
        f"{floor}, or correct `requires_version` if the pin is the thing that is wrong",
    )


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

    Asked of the branches a merge actually lands on, which since D16 are the
    plans' bases rather than `main`. Anchored to the default branch it reported
    on a branch nothing merges into and stayed quiet about the ones that do --
    green for a repository with a protected `main` and a bare plan branch,
    which is the wrong direction for a check whose premise is that dispatchkit
    might be the only gate there is.

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
    if not diagnostics.unprotected_bases:
        return Check(
            "merge-gate",
            True,
            "every branch a `verify: auto` merge targets requires status checks, "
            "so the merge is refused by GitHub as well as by dispatchkit",
        )
    bare = ", ".join(f"`{base}`" for base in diagnostics.unprotected_bases)
    return Check(
        "merge-gate",
        False,
        f"{bare} has no required status checks, so dispatchkit's own reading "
        "of CI is the only gate on a `verify: auto` merge into it",
        remedy=(
            f"add branch protection to {bare} requiring the check your "
            "`acceptance` command runs, so a red pull request is refused "
            "independently"
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
