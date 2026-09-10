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
lane = "cloud"
verify = "auto"
acceptance = "uv run pytest tests/unit -q"
touches = ["src/app/ports/**", "tests/unit/**"]

[[task]]
id = "fs-adapter"
title = "Implement the filesystem adapter"
milestone = "M2"
lane = "cloud"
acceptance = "uv run pytest tests/adapters/test_fs.py -q"
depends = [{ on = "ports", for = "the Protocol to implement" }]
touches = ["src/app/adapters/fs.py", "tests/adapters/test_fs.py"]

[[task]]
id = "s3-adapter"
title = "Implement the S3 adapter"
milestone = "M2"
lane = "cloud"
acceptance = "uv run pytest tests/adapters/test_s3.py -q"
depends = [{ on = "ports", for = "the Protocol to implement" }]
touches = ["src/app/adapters/s3.py", "tests/adapters/test_s3.py"]
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

The fence is the *only* path rule an unattended merge answers to. A task's own `touches` is a
scheduling hint: it keeps two tasks that would edit the same files from running at once, and a
pull request that goes outside it is reported so you can correct the graph — but it never
withholds a merge. A file list written before the work is a prediction, and predictions belong in
the scheduler rather than in a gate. What guards an unattended merge is green CI on a rebased
branch, plus the fence.

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

## Running work on your own machine

A task marked `lane = "local"` runs here rather than in the cloud sandbox, for the things a
sandbox cannot offer: a GPU, an OS, local data, unrestricted network, or simply more time than a
session cap allows. It is a capability escape hatch, not a way to go faster — parallelism is the
cloud lane's job.

```sh
dispatchkit watch --repo owner/name --push --local
dispatchkit doctor --local     # is the runner actually installed?
```

Without `--local`, a local task is never dispatched: it defers with `no-executor` and holds no
slot, because a lane nothing runs should say so rather than mark an issue and wait for ever.

One runs at a time, in its own `git worktree` under `~/.dispatchkit/work/`, branched from
`origin/main`. dispatchkit fetches, branches, builds the prompt from the issue's prose, runs the
agent, runs the task's `acceptance` command, pushes, and opens the pull request with `Closes #N`
— the agent is handed a prepared, disposable tree and asked to do exactly one thing. The child
process gets an explicit environment allowlist with no credential in it, so an agent-authored
`acceptance` command cannot reach your token or push anywhere; the push happens in the parent.

A failed run keeps its branch and its worktree so you can look at them, and returns the task to
the queue. Nothing in that branch is ever read back — the retry starts clean. Stopping `watch`
with Ctrl-C loses the run in progress; the next start finds the leftovers, preserves any commits,
and puts the task back.

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

While `watch` is waiting for its next pass it also watches `docs/plans/`. Save a graph and the
wait is cut short: the file is re-validated and re-linted, what it means is printed, and the next
pass runs immediately instead of at the end of the interval. Nothing is written to GitHub by a
save — `apply` stays the explicit act — and a file caught mid-edit is reported, not fatal.

`verify: auto` also needs its graph file committed. While a plan's file is dirty in your working
tree, that plan's tasks fall back to `verify: human` for the pass — the automatic merge authority
is withheld, but nothing is blocked and you can still merge by hand. Plans are independent, so an
unsaved experiment in one stops nothing in the others, and dispatch is never affected. Commit the
file and the next pass merges as before.

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
lane executor (D6) — until D6 lands a `lane = "local"` task
is deferred with `no-executor` and reserves nothing, rather than being marked for a listener that
is not there.
[docs/design.md](docs/design.md) carries the reasoning and a decision record per deliverable;
[docs/RESUME.md](docs/RESUME.md) has the next actions.

## Development

```sh
make check   # pytest, ruff, mypy --strict, import-linter
```

Tests are tiered: `unit` (pure), `replay` (recorded API payloads), and `live` (deselected by
default). Sockets are blocked for everything except `live`, so the offline guarantee is enforced
rather than asserted.
