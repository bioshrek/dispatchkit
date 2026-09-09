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

`watch` is that pass on a loop: it re-runs when you save a graph, otherwise on a backing-off
poll, and shows you what it is about to do. It covers every plan in the repository at once, so the
concurrency caps bound total work in flight rather than work per plan; `--once` runs a single pass
and exits. There is no cron and no GitHub Actions workflow — the scheduler is a command on your
machine, and nothing moves while it is not running.

## Three properties it is built around

**Assignment is the lock.** A task is ready only while it is unassigned, so dispatching it removes
it from the ready set. Two passes racing on the same repository converge instead of
double-dispatching — no lease, no lockfile, no database.

**There is no state.** Status is recomputed from the issues on every pass rather than stored, so
there is no state machine to get wedged, no board to go stale, and nothing to reconcile between
runs. Rate-limited halfway through? The next pass finishes the job.

**No dependencies, ever.** `dispatchkit` is pure standard library, and a test walks its imports to
keep it that way. The process that holds a token which can assign work runs no third-party code.

## Status

Early, and honest about it. Applying a graph, dispatching to the cloud agent, the retry budget and
`verify: auto` auto-merge are built and have run live against a real repository. Being built now,
in this order: retiring the Project board so `repo` scope is all you need (D14), `watch` itself
(D13), and the local lane executor (D6) — until they land, the commands above still take
`--project`, the scheduler is `dispatchkit tick`, and `lane = "local"` parks a task rather than
running it.
[docs/design.md](docs/design.md) carries the reasoning and a decision record per deliverable;
[docs/RESUME.md](docs/RESUME.md) has the next actions.

## Development

```sh
make check   # pytest, ruff, mypy --strict, import-linter
```

Tests are tiered: `unit` (pure), `replay` (recorded API payloads), and `live` (deselected by
default). Sockets are blocked for everything except `live`, so the offline guarantee is enforced
rather than asserted.
