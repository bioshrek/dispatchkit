# dispatchkit

Turn a reviewed, dependency-ordered task graph into GitHub issues, and hand each one to a coding
agent when — and only when — its prerequisites are actually done.

```toml
# docs/plans/refactor.tasks.toml
plan = "refactor"

[[task]]
id = "ports"
title = "Define the application ports"
milestone = "M2"
verify = "auto"
acceptance = "uv run pytest tests/unit -q"
touches = ["src/app/ports/**"]

[[task]]
id = "adapter"
title = "Implement the filesystem adapter"
milestone = "M2"
depends = [{ on = "ports", reason = "needs the Protocol to implement" }]
touches = ["src/app/adapters/**"]
```

```sh
dispatchkit init     --repo o/n --push                # labels, config, plans directory
dispatchkit doctor   --repo o/n                       # scopes, agent access, labels
dispatchkit validate docs/plans/refactor.tasks.toml   # shape, cycles, lints
dispatchkit apply    docs/plans/refactor.tasks.toml --push --repo o/n
dispatchkit watch    --repo o/n                       # the scheduler; Ctrl-C stops it
```

## Getting started

Setup is `gh auth login` and nothing else: `repo` scope, no personal access token pasted into a
secrets page, no board to provision. `init` is idempotent and safe to re-run; it never overwrites
a file that already exists. `doctor` reports what is still missing and, for each failure, the
command that fixes it — with no `--repo` it checks the working tree alone and never opens a
socket.

Settings live in `.github/dispatchkit.toml` (a root `dispatchkit.toml` is still read): the
per-lane concurrency caps, the retry budget, where task graphs live, and the blast-radius fence —
the paths auto-merge may never touch unattended, which defaults to the config and the graph files,
so the pipeline cannot rewrite its own rules while nobody is looking.

## What it actually does

One idempotent pass, safe to run as often as you like:

1. **Load** every `dispatchkit`-labelled issue in one GraphQL query.
2. **Resolve** each task's status — _ready_ means open, unassigned, and every dependency closed.
3. **Report** every task, not just the ones moving, so an idle pass explains itself.
4. **Admit** ready tasks in plan order, subject to per-lane concurrency caps and file-scope exclusion.
5. **Dispatch** — assign the coding agent (cloud lane), or prepare a worktree and run it here (local lane).

`watch` is that pass on a loop, re-running every 60 seconds (`--interval`) until you stop it. The
first pass prints the whole picture and later ones print only what moved, so a settled repository
is a heartbeat rather than the same block over and over. It covers every plan at once, so the
concurrency caps bound total work in flight rather than work per plan; `--once` runs a single pass,
prints the full report and exits. There is no cron and no GitHub
Actions workflow — the scheduler is a command on your machine, running as you, and nothing moves
while it is not running.

## Stopping a task, without a control plane

Every intervention is ordinary GitHub state, so it works from the CLI, the web UI or a phone, and
a pass picks it up next time round:

| You want                | Do this                                          | Its dependents            |
| ----------------------- | ------------------------------------------------ | ------------------------- |
| Stop this run, try again | Unassign the agent                                | Unaffected                |
| Not now                 | Unassign, and add `dispatch:hold`                 | Wait                      |
| Never                   | Close the issue as **not planned**                | Blocked forever, reported |
| I did it myself         | Close the issue normally                          | Released                  |

A hold stops dispatch *and* auto-merge, and does not spend one of the task's three attempts — you
interrupted it, the agent did not fail. Releasing it is removing the label, and nothing else:
nothing was written down when you added it. Closing an issue as not planned is the one that used
to be a trap; the tasks it strands are now named in the report rather than left looking merely
`Blocked`.

Label not there yet? `gh label create dispatch:hold --description "dispatchkit: not now"`.

## Three properties it is built around

**Assignment is the lock.** A task is ready only while it is unassigned, so dispatching it removes
it from the ready set. Two passes racing on the same repository converge instead of
double-dispatching — no lease, no lockfile, no database.

**There is no state.** Status is recomputed from the issues on every pass rather than stored, so
there is no state machine to get wedged, no board to go stale, and nothing to reconcile between
runs. Rate-limited halfway through? The next pass finishes the job.

**No dependencies, ever.** `dispatchkit` is pure standard library, and a test walks its imports to
keep it that way. The process that can assign work runs on your credential, so it runs no
third-party code.

## Status

Early, and honest about it. Applying a graph, dispatching to the cloud agent, the retry budget and
`verify: auto` auto-merge are built and have run live against a real repository. The Project board
is retired, so `repo` scope is all any command needs (D14), and the scheduler is now `watch` on
your own machine rather than a workflow holding a token (D13). Intervention landed with D13.1: a
task closed as not planned no longer unblocks its dependents, and `dispatch:hold` says "not now"
without spending a retry. Being built next: the graph watcher that re-plans on save, and the local
lane executor (D6) — until D6 lands, `lane = "local"`
parks a task rather than running it.
[docs/design.md](docs/design.md) carries the reasoning and a decision record per deliverable;
[docs/RESUME.md](docs/RESUME.md) has the next actions.

## Development

```sh
make check   # pytest, ruff, mypy --strict, import-linter
```

Tests are tiered: `unit` (pure), `replay` (recorded API payloads), and `live` (deselected by
default). Sockets are blocked for everything except `live`, so the offline guarantee is enforced
rather than asserted.
