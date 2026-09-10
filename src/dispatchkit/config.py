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
from datetime import timedelta
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

TOP_LEVEL_KEYS = frozenset({"caps", "retry", "paths", "fence", "runner"})
RETRY_KEYS = frozenset({"budget"})
PATHS_KEYS = frozenset({"plans"})
FENCE_KEYS = frozenset({"paths"})
RUNNER_KEYS = frozenset({"argv", "model", "models", "env", "timeout"})

#: The substitutions an argv template may use. Closed, like every other
#: grammar here: a template naming something else is a typo the adopter wants
#: to hear about, not an element that silently renders as itself.
PLACEHOLDERS = frozenset({"prompt", "prompt_file", "worktree", "model", "effort"})

#: What a local run inherits from the parent process.
#:
#: The local lane executes a task's `acceptance` on the workstation, from a
#: body an agent may have influenced, so the question is not "what might the
#: runner want" but "what may an agent-authored command reach". The floor is
#: what a program needs to find its files and speak to a terminal.
#:
#: `SSH_AUTH_SOCK` is absent deliberately, and it is the interesting one: with
#: no agent socket the child cannot authenticate to a remote at all, which is
#: why the executor pushes from the parent through `gh`. The credential and
#: the untrusted command never share a process.
DEFAULT_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL", "USER", "LOGNAME")

#: Names an adopter may not add to the allowlist, however they spell them.
#: An allowlist that can be told to allow the token is not an allowlist, and
#: this file is as editable by a pull request as any other.
SECRET_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "AUTH", "COOKIE", "SESSION")

#: The runner as shipped. `-p` takes the prompt *text*, so `{prompt}` is the
#: substitution rather than `{prompt_file}`; handing that flag a filename runs
#: whatever the filename happens to say.
DEFAULT_RUNNER_ARGV = (
    "copilot",
    "-p",
    "{prompt}",
    "--model",
    "{model}",
    "--autopilot",
    "--yolo",
    "--max-autopilot-continues",
    "20",
)
#: `auto` first, because the first is the default and a default is a promise
#: made to an account its author cannot see (D6.6). Model availability varies
#: by plan, by org policy and by month -- the local lane's first live agent run
#: died on `Model "claude-opus-5" is not available` -- so no specific name can
#: keep that promise, and `auto` is the CLI's documented way to ask for one
#: that works. The specific names stay, because this tuple is also the
#: allowlist a task's own `model` is checked against, and that is a trust
#: boundary for agent-authored graphs rather than a convenience.
DEFAULT_MODELS = ("auto", "claude-opus-5", "claude-sonnet-5")


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
class RunnerConfig:
    """How a local task is run: the command, the model, the environment.

    The command is a list and never a string. Substitution replaces whole
    elements, so `n` template elements produce exactly `n` arguments whatever
    the value contains — a prompt holding `; rm -rf /` is one argument that
    happens to have a semicolon in it, because nothing ever parses it again.
    """

    argv: tuple[str, ...] = DEFAULT_RUNNER_ARGV
    #: The model used when a task names none.
    model: str = DEFAULT_MODELS[0]
    #: What a task's own `model` may be set to. Empty means "no overrides":
    #: a graph file is agent-authorable, so the permissive reading of an
    #: unanswered question is the wrong one.
    models: tuple[str, ...] = DEFAULT_MODELS
    #: Added to `DEFAULT_ENV`, never replacing it.
    env: tuple[str, ...] = ()
    #: How long a child may run before the supervisor kills it. The hang case
    #: is a child-process problem, so it is solved with a child-process
    #: timeout rather than anything involving GitHub.
    timeout: timedelta = timedelta(hours=2)

    def allows(self, model: str) -> bool:
        return model in self.models

    def render(self, **values: str | None) -> list[str]:
        """Fill the template. One element in, one element out, always."""
        return [_substitute(element, values) for element in self.argv]

    def environment(self, parent: Mapping[str, str]) -> dict[str, str]:
        allowed = (*DEFAULT_ENV, *self.env)
        return {name: value for name, value in parent.items() if name in allowed}


def _substitute(element: str, values: Mapping[str, str | None]) -> str:
    rendered = element
    for name in PLACEHOLDERS:
        token = "{" + name + "}"
        if token not in rendered:
            continue
        value = values.get(name)
        if value is None:
            # Dropping the element would change the argument count and an
            # empty string in its place is a different command from the one
            # written. Neither is ours to choose.
            raise GraphError(
                [
                    GraphIssue(
                        "missing-substitution",
                        element,
                        f"the runner template needs `{name}`, which this task does not set",
                    )
                ]
            )
        rendered = rendered.replace(token, value)
    return rendered


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    caps: Mapping[Lane, int] = field(default_factory=lambda: dict(DEFAULT_CAPS))
    retry_budget: int = 3
    #: How long a dispatch may produce nothing before it is reclaimed (D7).
    stall_after: timedelta = timedelta(hours=24)
    plans: Path = DEFAULT_PLANS
    fence: tuple[str, ...] = field(
        default_factory=lambda: default_fence(DEFAULT_LOCATIONS[0], DEFAULT_PLANS)
    )
    runner: RunnerConfig = field(default_factory=RunnerConfig)

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
    runner = _read_runner(document, path, issues)

    if issues:
        raise GraphError(issues)
    return SchedulerConfig(
        caps=caps, retry_budget=budget, plans=plans, fence=fence, runner=runner
    )


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
        if lane is Lane.LOCAL and value > 1:
            # An invariant, not a default. More local parallelism is never
            # what is actually wanted -- it is evidence the task did not need
            # this machine -- and a silently clamped cap would suppress the
            # feedback the lane exists to collect.
            issues.append(
                GraphIssue(
                    "local-cap-invariant",
                    str(path),
                    "`caps.local` is fixed at 1: the local lane is a capability escape "
                    "hatch, not a throughput mechanism. If tasks are queueing, drop the "
                    "`requires` that pinned them here and let the cloud lane run them.",
                )
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


def _read_runner(
    document: Mapping[str, object], path: Path, issues: list[GraphIssue]
) -> RunnerConfig:
    runner = _table(document, "runner")
    issues.extend(
        GraphIssue("unknown-key", str(path), f"unknown `runner` key `{key}`")
        for key in sorted(set(runner) - RUNNER_KEYS)
    )
    argv = _read_argv(runner, path, issues)
    models = _read_strings(runner, "models", DEFAULT_MODELS, path, issues)
    env = _read_env(runner, path, issues)
    # An explicit `models` supplies the default as well as the allowlist
    # (D6.6): narrowing the list and leaving `model` alone is the obvious way
    # to say "only these", and taking the global default here would turn that
    # into `runner.model is not listed by runner.models` — the config being
    # made to disagree with itself by a key the author never wrote. An empty
    # list means "a task may not choose" and names nothing, so it cannot.
    fallback = models[0] if models else DEFAULT_MODELS[0]
    model = runner.get("model", fallback)
    if not isinstance(model, str) or not model:
        issues.append(GraphIssue("invalid-type", str(path), "`runner.model` must be a string"))
        model = fallback
    elif models and model not in models:
        # A default outside its own allowlist is the config disagreeing with
        # itself, and it fails on the first local task rather than here.
        issues.append(
            GraphIssue(
                "invalid-value",
                str(path),
                f"`runner.model` is {model!r}, which `runner.models` does not list",
            )
        )
    return RunnerConfig(argv=argv, model=model, models=models, env=env)


def _read_argv(
    runner: Mapping[str, object], path: Path, issues: list[GraphIssue]
) -> tuple[str, ...]:
    if "argv" not in runner:
        return DEFAULT_RUNNER_ARGV
    argv = runner["argv"]
    if (
        not isinstance(argv, Sequence)
        or isinstance(argv, str)
        or not argv
        or not all(isinstance(element, str) for element in argv)
    ):
        # A string here is the one spelling that would reintroduce a shell,
        # so it is refused by type rather than split on whitespace.
        issues.append(
            GraphIssue(
                "invalid-type",
                str(path),
                "`runner.argv` must be a non-empty list of strings, so that the command "
                "is never parsed by a shell",
            )
        )
        return DEFAULT_RUNNER_ARGV
    rendered = tuple(str(element) for element in argv)
    issues.extend(
        GraphIssue(
            "unknown-placeholder",
            str(path),
            f"`runner.argv` uses {{{name}}}, which is not one of: "
            + ", ".join(sorted(PLACEHOLDERS)),
        )
        for name in sorted(_placeholders(rendered) - PLACEHOLDERS)
    )
    return rendered


def _placeholders(argv: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for element in argv:
        rest = element
        while "{" in rest and "}" in rest[rest.index("{") :]:
            start = rest.index("{")
            end = rest.index("}", start)
            found.add(rest[start + 1 : end])
            rest = rest[end + 1 :]
    return found


def _read_env(
    runner: Mapping[str, object], path: Path, issues: list[GraphIssue]
) -> tuple[str, ...]:
    env = _read_strings(runner, "env", (), path, issues)
    for name in env:
        if any(marker in name.upper() for marker in SECRET_MARKERS):
            issues.append(
                GraphIssue(
                    "refused-env",
                    str(path),
                    f"`runner.env` may not name {name!r}: the local lane runs "
                    "agent-authored commands, and an allowlist that can be told to allow "
                    "a credential is not an allowlist",
                )
            )
    return env


def _read_strings(
    table: Mapping[str, object],
    key: str,
    fallback: tuple[str, ...],
    path: Path,
    issues: list[GraphIssue],
) -> tuple[str, ...]:
    if key not in table:
        return fallback
    value = table[key]
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str)
        or not all(isinstance(element, str) for element in value)
    ):
        issues.append(
            GraphIssue("invalid-type", str(path), f"`runner.{key}` must be a list of strings")
        )
        return fallback
    return tuple(str(element) for element in value)


def _table(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    return value if isinstance(value, Mapping) else {}


def _posix(path: Path) -> str:
    """A repository-relative path, comparably spelled: no `./`, no backslashes."""
    return str(PurePosixPath(*Path(path).parts))
