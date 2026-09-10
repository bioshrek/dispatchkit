"""D5.5: `dispatchkit init` — make a repository able to run a pass.

`doctor` names what is missing; this creates it: the labels the state query
filters on, the plans directory and the config file.

One command, not two. The `--local` split was made at the token boundary
rather than the dry-run boundary, and that boundary *was* the `project` scope
the board needed (D14). What is left needs `gh`'s ordinary credential and only
creates labels, so `--repo` alone decides whether the remote half happens —
the same shape as `doctor`.

Held to the same standard as `apply`, for the same reason — it runs against a
repository somebody may already be using:

- **The plan is data.** `plan_init` is pure; `execute_init` is the only half
  that touches anything.
- **Convergence is the test.** Apply the plan, re-read, re-plan, and the
  second plan must be empty.
- **Nothing that exists is overwritten.** A file that is already there is left
  exactly as it is, because somebody else's config is not ours to rewrite.
- **Success is a verdict, not a report of effort** (D11). Those last two rules
  combine into a hole: over an existing config with a fence gap, `init` steps
  over the file, creates nothing and exits 0, while `doctor` on the same tree
  exits 1. So `init` ends by re-reading the tree it leaves behind and running
  the local checks against it. The gate is a postcondition, not a
  precondition — the labels and the plans directory are a gain whatever else
  is wrong, and withholding them would only add a second failing check.

The config template below is this repository's own, pinned by a test, so what
`init` writes into an adopter's tree is the file this repository runs on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dispatchkit.doctor import LocalFacts
from dispatchkit.github import Notice, missing_labels


class LabelApi(Protocol):
    """The only remote capability `init` needs, and deliberately the whole of it.

    Kept separate from `GitHubApi` rather than reusing it: setting a repository
    up and running a scheduler pass are different jobs with different blast
    radii, and setup should not be handed a client that can assign work.
    """

    def ensure_labels(self, labels: Sequence[str]) -> None: ...


@dataclass(frozen=True, slots=True)
class CreateLabel:
    name: str


@dataclass(frozen=True, slots=True)
class WriteFile:
    path: Path
    content: str


@dataclass(frozen=True, slots=True)
class MakeDirectory:
    path: Path


InitOperation = CreateLabel | WriteFile | MakeDirectory


@dataclass(frozen=True, slots=True)
class InitPlan:
    operations: tuple[InitOperation, ...]
    notices: tuple[Notice, ...]

    def __bool__(self) -> bool:
        return bool(self.operations)


@dataclass(frozen=True, slots=True)
class InitResult:
    labels: int
    files: int
    directories: int


def plan_init(labels: Sequence[str], facts: LocalFacts) -> InitPlan:
    """What this repository still needs. Pure: no network, no filesystem.

    `labels` is what the repository already defines. Without a credential it is
    empty, which plans every label — and creating one that exists is a no-op,
    so the offline plan is a superset of the online one rather than a wrong one.
    """
    operations: list[InitOperation] = []
    notices: list[Notice] = []

    operations += [CreateLabel(name) for name in missing_labels(labels)]

    if not facts.plans_exists:
        operations.append(MakeDirectory(facts.plans))
    if not facts.config_exists:
        operations.append(WriteFile(facts.config_path, CONFIG_TEMPLATE))

    return InitPlan(tuple(operations), tuple(notices))


def execute_init(plan: InitPlan, api: LabelApi | None) -> InitResult:
    """Carry out the whole plan. `api` is `None` when there is no credential."""
    labels = execute_labels(plan, api) if api is not None else 0
    files, directories = execute_tree(plan)
    return InitResult(labels=labels, files=files, directories=directories)


def execute_labels(plan: InitPlan, api: LabelApi) -> int:
    """The half that needs a credential."""
    # One call, not one per label: the adapter batches them, and
    # `gh label create --force` is idempotent.
    labels = [op.name for op in plan.operations if isinstance(op, CreateLabel)]
    if labels:
        api.ensure_labels(labels)
    return len(labels)


def execute_tree(plan: InitPlan) -> tuple[int, int]:
    """The half that needs no token, and so can run before one exists.

    The paths in the plan are the paths that get written. Resolving a root
    here as well as in the facts the plan was made from is how a relative
    root ends up written twice; the facts own that join, and only the facts.
    """
    directories = [op.path for op in plan.operations if isinstance(op, MakeDirectory)]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    files = [op for op in plan.operations if isinstance(op, WriteFile)]
    for operation in files:
        path = operation.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            continue  # a file that appeared since the plan was made is not ours
        path.write_text(operation.content, encoding="utf-8")
    return len(files), len(directories)


def summarise(plan: InitPlan) -> list[str]:
    lines = [f"  {type(op).__name__} {_subject(op)}" for op in plan.operations]
    lines += [f"NOTE {notice}" for notice in plan.notices]
    lines += [f"NEXT {step}" for step in NEXT_STEPS]
    lines += [f"MANUAL {step}" for step in MANUAL_STEPS]
    return lines


#: What `init` cannot do for the adopter, said at the point they would
#: otherwise walk away believing the setup is finished. Since D13 there is no
#: token to install and no plan variable to set — the scheduler runs as the
#: adopter, from their own terminal — so only the branch protection is left,
#: and it is left manual because guessing somebody's check name is worse than
#: naming the gap.
NEXT_STEPS = (
    "gh api -X PUT repos/<owner>/<repo>/branches/main/protection ...  (require the "
    "status check your `acceptance` command runs; without it dispatchkit's own "
    "reading of CI is the only gate on a `verify: auto` merge)",
)

#: Kept apart from `NEXT_STEPS` because there is no command to copy: the
#: setting is not exposed over REST, so neither `init` nor `doctor` can act on
#: it or even read it back. Until it is off, every agent-authored CI run waits
#: for a human click, which is precisely what `verify: auto` promises not to
#: need. Stated with its cost, because it is a real trust decision.
MANUAL_STEPS = (
    'for `verify: auto`: Settings > Copilot > Cloud agent > "Actions workflow '
    'approval" > turn off "Require approval for workflow runs". Until then every '
    "agent-authored CI run waits for a human. Turning it off lets unreviewed "
    "agent code run your workflows, including changes to .github/workflows/.",
)


def _subject(operation: InitOperation) -> str:
    if isinstance(operation, CreateLabel):
        return operation.name
    return str(operation.path)


#: This repository's own config, and what `init` writes. Pinned by a test.
CONFIG_TEMPLATE = r"""# Scheduler settings for `dispatchkit`. Every key here has a default, so this
# file is optional — it exists to make the defaults visible and reviewable.
#
# `dispatchkit doctor` reports which file it was read from.

[caps]
# Concurrent cloud agent tasks. Bounded by review capacity, not by API limits:
# more open PRs than a person can read is a queue, not throughput.
cloud = 3
# Concurrent local tasks. Fixed at 1, and `dispatchkit` refuses a larger value:
# the local lane is a capability escape hatch, not a throughput mechanism, so
# tasks queueing here is a signal to drop the `requires` that pinned them
# rather than a reason for another slot.
local = 1

[retry]
# Attempts before a task is labelled `dispatch:stuck` and left alone.
budget = 3

[paths]
# Where committed task graphs live: `<plans>/<plan>.tasks.toml`.
plans = "docs/plans"

[runner]
# What runs a `lane = "local"` task on this machine. A list, never a string:
# substitution replaces whole elements, so `n` elements here are `n` arguments
# there whatever a value contains, and nothing is ever parsed by a shell.
#
# Substitutions: {prompt} {prompt_file} {worktree} {model} {effort}
# `-p` takes the prompt text, so {prompt} is the right one; a CLI that reads a
# file wants {prompt_file}. The child runs with the worktree as its cwd.
argv = [
    "copilot",
    "-p",
    "{prompt}",
    "--model",
    "{model}",
    "--autopilot",
    "--yolo",
    "--max-autopilot-continues",
    "20",
]

# The model used when a task names none, and what a task may name instead.
# An empty `models` means tasks may not choose at all — graph files are
# agent-authorable, so this list is the whole of that trust boundary.
model = "claude-opus-5"
models = ["claude-opus-5", "claude-sonnet-5"]

# Extra environment variables the runner needs, added to the floor of
# PATH, HOME, LANG, LC_ALL, TERM, TMPDIR, SHELL, USER, LOGNAME.
#
# The floor is short on purpose: the local lane runs a task's `acceptance` on
# this machine from an issue body an agent may have influenced. `gh`'s
# credential is not in scope, and neither is SSH_AUTH_SOCK — the child cannot
# authenticate to a remote at all, because the executor pushes from the parent.
# Naming a credential here is refused rather than honoured.
env = []

# The blast-radius fence: paths auto-merge must never touch unattended, because
# the pipeline may not rewrite its own rules, its own routing or its own merge
# permissions without a human. Omit the section to get the default, which is
# derived from the settings above:
#
#   [fence]
#   paths = [
#       ".github/workflows/**",
#       ".github/dispatchkit.toml",
#       "dispatchkit.toml",
#       "docs/plans/*.tasks.toml",
#   ]
#
# Setting it replaces that list rather than adding to it.
"""
