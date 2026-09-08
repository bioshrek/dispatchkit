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
dispatchkit init     --repo o/n --project 3 --push    # board, labels, config, workflow
dispatchkit doctor   --repo o/n --project 3           # scopes, agent, fields, labels
dispatchkit validate docs/plans/refactor.tasks.toml   # shape, cycles, lints
dispatchkit apply    docs/plans/refactor.tasks.toml --push --repo o/n --project 3
dispatchkit tick     --plan refactor --push --repo o/n --project 3
```

## Getting started

`init` is idempotent and safe to re-run; it never overwrites a file that already exists. Its
`--local` form writes the config, the workflow and the plans directory without touching a board,
which is the half that works before `gh auth refresh -s project` has been run. `doctor` reports
what is still missing and, for each failure, the command that fixes it — with no `--repo` it
checks the working tree alone and never opens a socket.

Settings live in `.github/dispatchkit.toml` (a root `dispatchkit.toml` is still read): the
per-lane concurrency caps, the retry budget, where task graphs live, and the blast-radius fence —
the paths auto-merge may never touch unattended, which defaults to the workflow, the config and
the graph files, so the pipeline cannot rewrite its own rules while nobody is looking.

## What it actually does

One idempotent pass, safe to run as often as you like:

1. **Load** every `dispatchkit`-labelled issue in one GraphQL query.
2. **Resolve** each task's status — *ready* means open, unassigned, and every dependency closed.
3. **Reconcile** the Project board, which is a derived view: delete it and the next pass rebuilds it.
4. **Admit** ready tasks in plan order, subject to per-lane concurrency caps and file-scope exclusion.
5. **Dispatch** — assign the coding agent (cloud lane), or label the issue for a local daemon.

## Three properties it is built around

**Assignment is the lock.** A task is ready only while it is unassigned, so dispatching it removes
it from the ready set. Two passes racing on the same repository converge instead of
double-dispatching — no lease, no lockfile, no database.

**There is no state.** Status is recomputed from the issues on every pass rather than stored, so
there is no state machine to get wedged and nothing to go stale between runs. Rate-limited
halfway through? The next pass finishes the job.

**No dependencies, ever.** `dispatchkit` is pure standard library, and a test walks its imports to
keep it that way. The job that holds a token which can assign work runs no third-party code.

## Status

Early. D1–D5.5 of the plan in [docs/design.md](docs/design.md) are implemented and covered
offline; the first live end-to-end run has not happened yet. The local daemon (D6), retry/reclaim
(D7), alerting (D8) and `verify: auto` merge (D9) are designed but unbuilt.
[docs/RESUME.md](docs/RESUME.md) has the current state and the next actions.

## Development

```sh
make check   # pytest, ruff, mypy --strict, import-linter
```

Tests are tiered: `unit` (pure), `replay` (recorded API payloads), and `live` (deselected by
default). Sockets are blocked for everything except `live`, so the offline guarantee is enforced
rather than asserted.
