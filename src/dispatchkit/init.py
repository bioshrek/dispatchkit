"""D5.5: `dispatchkit init` — make a repository able to run a pass.

`doctor` names what is missing; this creates it: the board's fields and
options, the labels the state query filters on, the plans directory, the
config file and the scheduler workflow.

Held to the same standard as `apply`, for the same reason — it runs against a
board somebody may already be using:

- **The plan is data.** `plan_init` is pure; `execute_init` is the only half
  that touches anything.
- **Convergence is the test.** Apply the plan, re-read, re-plan, and the
  second plan must be empty.
- **Nothing that exists is overwritten.** A file that is already there is left
  exactly as it is, because somebody else's config is not ours to rewrite.

One case earns its own rule. A new Project ships a built-in `Status` field
carrying `Todo`/`In Progress`/`Done`: the right name, the wrong options, and
no way to add options to an existing single select through the CLI. Fixing it
means deleting the field, which deletes its values — free on an empty board,
destructive on a populated one. So the board's item count decides, and a
populated board gets a notice naming the command rather than an operation
someone did not ask for.

The templates below are this repository's own config and workflow, pinned by a
test. What `init` writes into an adopter's tree is therefore the same file
`tests/test_workflow.py` asserts the permissions and script safety of.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dispatchkit.board import (
    BoardApi,
    BoardSnapshot,
    FieldSpec,
    mismatched_fields,
    missing_fields,
    missing_labels,
)
from dispatchkit.doctor import LocalFacts
from dispatchkit.github import Notice

#: Where the scheduler workflow lives. A path, not a convention: the file has
#: to be under `.github/workflows/` for GitHub to run it at all.
WORKFLOW_PATH = Path(".github/workflows/dispatchkit.yml")


@dataclass(frozen=True, slots=True)
class CreateField:
    spec: FieldSpec


@dataclass(frozen=True, slots=True)
class RecreateField:
    """Delete then create: the only way to fix a single select's options.

    Only ever planned for an empty board, where the values it drops are none.
    """

    spec: FieldSpec
    field_id: str


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


InitOperation = CreateField | RecreateField | CreateLabel | WriteFile | MakeDirectory


@dataclass(frozen=True, slots=True)
class InitPlan:
    operations: tuple[InitOperation, ...]
    notices: tuple[Notice, ...]

    def __bool__(self) -> bool:
        return bool(self.operations)


@dataclass(frozen=True, slots=True)
class InitResult:
    fields: int
    labels: int
    files: int
    directories: int


def plan_init(board: BoardSnapshot, facts: LocalFacts) -> InitPlan:
    """What this repository still needs. Pure: no board, no filesystem."""
    operations: list[InitOperation] = []
    notices: list[Notice] = []

    for spec, existing in mismatched_fields(board):
        wanted = [option for option in spec.options if option not in existing.options]
        if board.items == 0:
            operations.append(RecreateField(spec, existing.id))
            continue
        notices.append(
            Notice(
                "field-options",
                spec.name,
                f"`{spec.name}` cannot hold {_quoted(wanted)}, and the board has "
                f"{board.items} item(s) whose values a rebuild would delete. Fix it by hand: "
                f"`gh project field-delete --id {existing.id}`, then re-run init",
            )
        )

    operations += [CreateField(spec) for spec in missing_fields(board)]
    operations += [CreateLabel(name) for name in missing_labels(board)]

    if not facts.plans_exists:
        operations.append(MakeDirectory(facts.plans))
    if not facts.config_exists:
        operations.append(WriteFile(facts.config_path, CONFIG_TEMPLATE))
    if not facts.workflow_exists:
        operations.append(WriteFile(facts.workflow_path, WORKFLOW_TEMPLATE))

    return InitPlan(tuple(operations), tuple(notices))


def execute_init(plan: InitPlan, api: BoardApi) -> InitResult:
    """Carry out the whole plan."""
    fields, labels = execute_board(plan, api)
    files, directories = execute_tree(plan)
    return InitResult(fields=fields, labels=labels, files=files, directories=directories)


def execute_board(plan: InitPlan, api: BoardApi) -> tuple[int, int]:
    """The half that needs a token. Ordering matters here exactly once.

    A recreated field is deleted before it is created, because two fields
    cannot share a name.
    """
    fields = 0
    for operation in plan.operations:
        if isinstance(operation, RecreateField):
            api.delete_field(field_id=operation.field_id)
            api.create_field(operation.spec)
            fields += 1
        elif isinstance(operation, CreateField):
            api.create_field(operation.spec)
            fields += 1

    # One call, not one per label: the adapter batches them, and
    # `gh label create --force` is idempotent.
    labels = [op.name for op in plan.operations if isinstance(op, CreateLabel)]
    if labels:
        api.ensure_labels(labels)
    return fields, len(labels)


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
    return lines


def _subject(operation: InitOperation) -> str:
    if isinstance(operation, CreateField | RecreateField):
        return operation.spec.name
    if isinstance(operation, CreateLabel):
        return operation.name
    return str(operation.path)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)


#: This repository's own config, and what `init` writes. Pinned by a test.
CONFIG_TEMPLATE = r"""# Scheduler settings for `dispatchkit`. Every key here has a default, so this
# file is optional — it exists to make the defaults visible and reviewable.
#
# `dispatchkit doctor` reports which file it was read from.

[caps]
# Concurrent cloud agent tasks. Bounded by review capacity, not by API limits:
# more open PRs than a person can read is a queue, not throughput.
cloud = 3
# Concurrent local tasks. One, because the local daemon shares a working tree.
local = 1

[retry]
# Attempts before a task is labelled `dispatch:stuck` and left alone.
budget = 3

[paths]
# Where committed task graphs live: `<plans>/<plan>.tasks.toml`.
plans = "docs/plans"

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

#: This repository's own scheduler workflow, and what `init` writes.
WORKFLOW_TEMPLATE = r"""name: dispatchkit scheduler

# One idempotent scheduler pass: resolve every task's status from the issues,
# tell the board, and hand out whatever the caps allow. Event-driven for
# latency; cron as the self-healing safety net, offset off :00/:30 because the
# alert platform throttles hardest on the hour and the half hour.
on:
  issues:
    types: [closed, reopened, labeled]
  pull_request:
    types: [closed]
  schedule:
    - cron: "7,37 * * * *"
  workflow_dispatch:

# Least privilege. Never `contents: write` — only pull requests mutate the
# tree, so a compromised pass cannot commit.
permissions:
  issues: write
  repository-projects: write
  contents: read

# One pass at a time. Not cancel-in-progress: interrupting between assigning an
# issue and recording it is harmless but pointless, and the next pass would
# only have to redo it.
concurrency:
  group: dispatchkit-dispatch
  cancel-in-progress: false

jobs:
  tick:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # `src/dispatchkit` is pure standard library, so the pass installs
      # nothing: no dependency resolution to fail, no third-party code in a
      # job that holds a token which can assign work.
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # Values arrive as environment variables, never as `${{ }}` inside the
      # script: expressions are substituted before the shell parses the line,
      # so a value carrying a quote would become a command.
      - name: Scheduler pass
        env:
          # `src` on the path rather than an install step: there is nothing to
          # install, and `pip install` would be a build the pass depends on.
          PYTHONPATH: src
          GH_TOKEN: ${{ secrets.DISPATCHKIT_TOKEN }}
          REPO: ${{ github.repository }}
          PLAN: ${{ vars.DISPATCHKIT_PLAN }}
          PROJECT: ${{ vars.DISPATCHKIT_PROJECT }}
        run: |
          python -m dispatchkit tick \
            --plan "$PLAN" \
            --repo "$REPO" \
            --project "$PROJECT" \
            --push
"""
