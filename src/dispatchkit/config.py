"""Scheduler configuration (`.github/dispatchkit.toml`).

Four things live here, and all four are decisions an adopter owns rather than
decisions this tool gets to make for them:

- **Concurrency caps**, which keep review load bounded and stop PRs stacking
  into conflict.
- **The retry budget**, which turns a repeatedly failing task into a human's
  problem instead of an infinite loop.
- **The plans directory**, because `docs/plans/` is this repository's habit,
  not a law.
- **The blast-radius fence**, the set of paths auto-merge (D9) must never
  touch unattended. It defaults to covering *this tool's own inputs* — the
  workflow, the config file it was actually loaded from, and the graph files —
  so the pipeline cannot rewrite its own rules while nobody is looking.

The schema is closed, like the task graph's: an unknown key is an error, not a
silently ignored typo that leaves a cap at its default.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from dispatchkit.errors import GraphError, GraphIssue
from dispatchkit.model import Lane

DEFAULT_CAPS: Mapping[Lane, int] = {Lane.CLOUD: 3, Lane.LOCAL: 1}

#: Searched in order. `.github/` is the home; the repository root is the
#: fallback, kept working because the first adopters put it there.
DEFAULT_LOCATIONS = (Path(".github/dispatchkit.toml"), Path("dispatchkit.toml"))
DEFAULT_PLANS = Path("docs/plans")
GRAPH_SUFFIX = ".tasks.toml"

TOP_LEVEL_KEYS = frozenset({"caps", "retry", "paths", "fence"})
RETRY_KEYS = frozenset({"budget"})
PATHS_KEYS = frozenset({"plans"})
FENCE_KEYS = frozenset({"paths"})


def find_config(root: Path = Path()) -> Path:
    """The config file this repository uses, or where one should be written."""
    for location in DEFAULT_LOCATIONS:
        candidate = root / location
        if candidate.exists():
            return candidate
    return root / DEFAULT_LOCATIONS[0]


def default_fence(config_path: Path, plans: Path) -> tuple[str, ...]:
    """What the pipeline may not merge unattended, derived from its own inputs.

    Both documented config locations are fenced, not just the one in use: a
    config that moves house must not be able to arrive unreviewed. A config
    read from somewhere else entirely is added to the list, unless it was
    given as an absolute path, which is not a repository path at all.
    """
    patterns = [".github/workflows/**"]
    patterns += [_posix(location) for location in DEFAULT_LOCATIONS]
    if not config_path.is_absolute() and _posix(config_path) not in patterns:
        patterns.append(_posix(config_path))
    patterns.append(f"{_posix(plans)}/*{GRAPH_SUFFIX}")
    return tuple(patterns)


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    caps: Mapping[Lane, int] = field(default_factory=lambda: dict(DEFAULT_CAPS))
    retry_budget: int = 3
    plans: Path = DEFAULT_PLANS
    fence: tuple[str, ...] = field(
        default_factory=lambda: default_fence(DEFAULT_LOCATIONS[0], DEFAULT_PLANS)
    )

    def cap(self, lane: Lane) -> int:
        """A lane with no configured cap admits nothing — fail closed."""
        return self.caps.get(lane, 0)

    def graph_path(self, plan: str) -> Path:
        return self.plans / f"{plan}{GRAPH_SUFFIX}"

    def is_fenced(self, path: str) -> bool:
        """Does this repository path fall inside the blast-radius fence?

        `fnmatch`'s `*` crosses `/`, so a pattern matches more paths than a
        shell glob would. That errs toward fencing, which is the safe
        direction for a rule whose only job is to withhold a merge.
        """
        candidate = _posix(Path(path))
        return any(fnmatch(candidate, pattern) for pattern in self.fence)


def load_config(path: Path | None = None) -> SchedulerConfig:
    """Read the config file, falling back to defaults when it does not exist."""
    path = find_config() if path is None else path
    if not path.exists():
        return SchedulerConfig(fence=default_fence(path, DEFAULT_PLANS))

    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise GraphError([GraphIssue("invalid-toml", str(path), str(exc))]) from exc

    issues = [
        GraphIssue("unknown-key", str(path), f"unknown top-level key `{key}`")
        for key in sorted(set(document) - TOP_LEVEL_KEYS)
    ]

    caps = _read_caps(document, path, issues)
    budget = _read_budget(document, path, issues)
    plans = _read_plans(document, path, issues)
    fence = _read_fence(document, path, plans, issues)

    if issues:
        raise GraphError(issues)
    return SchedulerConfig(caps=caps, retry_budget=budget, plans=plans, fence=fence)


def _read_caps(
    document: Mapping[str, object], path: Path, issues: list[GraphIssue]
) -> dict[Lane, int]:
    caps = dict(DEFAULT_CAPS)
    for name, value in _table(document, "caps").items():
        try:
            lane = Lane(name)
        except ValueError:
            issues.append(GraphIssue("invalid-enum", str(path), f"unknown lane `{name}`"))
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            issues.append(
                GraphIssue("invalid-type", str(path), f"cap for `{name}` must be a count >= 0")
            )
            continue
        caps[lane] = value
    return caps


def _read_budget(document: Mapping[str, object], path: Path, issues: list[GraphIssue]) -> int:
    retry = _table(document, "retry")
    issues.extend(
        GraphIssue("unknown-key", str(path), f"unknown `retry` key `{key}`")
        for key in sorted(set(retry) - RETRY_KEYS)
    )
    budget = retry.get("budget", 3)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        issues.append(GraphIssue("invalid-type", str(path), "`retry.budget` must be an int >= 1"))
        return 3
    return budget


def _read_plans(document: Mapping[str, object], path: Path, issues: list[GraphIssue]) -> Path:
    paths = _table(document, "paths")
    issues.extend(
        GraphIssue("unknown-key", str(path), f"unknown `paths` key `{key}`")
        for key in sorted(set(paths) - PATHS_KEYS)
    )
    plans = paths.get("plans", str(DEFAULT_PLANS))
    if not isinstance(plans, str) or not plans:
        issues.append(
            GraphIssue("invalid-type", str(path), "`paths.plans` must be a directory string")
        )
        return DEFAULT_PLANS
    candidate = Path(plans)
    # A graph path is joined onto the repository root and handed to a reader;
    # an absolute or climbing path is a configuration mistake worth naming, not resolving.
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(
            GraphIssue(
                "invalid-type",
                str(path),
                f"`paths.plans` must be inside the repository, got {plans!r}",
            )
        )
        return DEFAULT_PLANS
    return candidate


def _read_fence(
    document: Mapping[str, object], path: Path, plans: Path, issues: list[GraphIssue]
) -> tuple[str, ...]:
    if "fence" not in document:
        return default_fence(path, plans)
    fence = _table(document, "fence")
    issues.extend(
        GraphIssue("unknown-key", str(path), f"unknown `fence` key `{key}`")
        for key in sorted(set(fence) - FENCE_KEYS)
    )
    if "paths" not in fence:
        return default_fence(path, plans)
    patterns = fence["paths"]
    if not isinstance(patterns, Sequence) or isinstance(patterns, str):
        issues.append(
            GraphIssue("invalid-type", str(path), "`fence.paths` must be a list of patterns")
        )
        return default_fence(path, plans)
    if not all(isinstance(pattern, str) for pattern in patterns):
        issues.append(GraphIssue("invalid-type", str(path), "`fence.paths` must be strings"))
        return default_fence(path, plans)
    return tuple(str(pattern) for pattern in patterns)


def _table(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    return value if isinstance(value, Mapping) else {}


def _posix(path: Path) -> str:
    """A repository-relative path, comparably spelled: no `./`, no backslashes."""
    return str(PurePosixPath(*Path(path).parts))
