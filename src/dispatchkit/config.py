"""Scheduler configuration (`dispatchkit.toml`).

Concurrency caps are the dial that keeps review load bounded and stops PRs
stacking into conflict, and the retry budget is what turns a repeatedly failing
task into a human's problem instead of an infinite loop. Both are guesses that
want tuning against real behaviour, so they live in a file rather than in code.

The schema is closed, like the task graph's: an unknown key is an error, not a
silently ignored typo that leaves a cap at its default.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dispatchkit.errors import GraphError, GraphIssue
from dispatchkit.model import Lane

DEFAULT_CAPS: Mapping[Lane, int] = {Lane.CLOUD: 3, Lane.LOCAL: 1}
DEFAULT_PATH = Path("dispatchkit.toml")

TOP_LEVEL_KEYS = frozenset({"caps", "retry"})
RETRY_KEYS = frozenset({"budget"})


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    caps: Mapping[Lane, int] = field(default_factory=lambda: dict(DEFAULT_CAPS))
    retry_budget: int = 3

    def cap(self, lane: Lane) -> int:
        """A lane with no configured cap admits nothing — fail closed."""
        return self.caps.get(lane, 0)


def load_config(path: Path = DEFAULT_PATH) -> SchedulerConfig:
    """Read `dispatchkit.toml`, falling back to defaults when it does not exist."""
    if not path.exists():
        return SchedulerConfig()

    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise GraphError([GraphIssue("invalid-toml", str(path), str(exc))]) from exc

    issues = [
        GraphIssue("unknown-key", str(path), f"unknown top-level key `{key}`")
        for key in sorted(set(document) - TOP_LEVEL_KEYS)
    ]

    caps = dict(DEFAULT_CAPS)
    for name, value in (document.get("caps") or {}).items():
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

    retry = document.get("retry") or {}
    issues.extend(
        GraphIssue("unknown-key", str(path), f"unknown `retry` key `{key}`")
        for key in sorted(set(retry) - RETRY_KEYS)
    )
    budget = retry.get("budget", 3)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        issues.append(GraphIssue("invalid-type", str(path), "`retry.budget` must be an int >= 1"))
        budget = 3

    if issues:
        raise GraphError(issues)
    return SchedulerConfig(caps=caps, retry_budget=budget)
