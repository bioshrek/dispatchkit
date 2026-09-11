---
name: dispatchkit-planning
description: Turn a piece of work into a reviewed, dependency-ordered dispatchkit plan — a *.tasks.toml graph plus one brief per task — that `dispatchkit validate --strict` accepts. Use when asked to decompose work into parallel tasks for coding agents, to write or revise a plan file, or to fix what `validate` reports.
---

# Authoring a dispatchkit plan

You are writing a **plan**: one `*.tasks.toml` file describing tasks and the dependencies between
them, plus one short Markdown brief per task. A scheduler turns each task into a GitHub issue and
hands it to a coding agent once its prerequisites are genuinely closed.

## Your output

- One graph file, conventionally `docs/plans/<name>.tasks.toml`.
- One brief per task, referenced by `body_file`.

## What you may run, and what you must not

Check your own work, as many times as you like:

```sh
dispatchkit validate --strict docs/plans/<name>.tasks.toml
```

It reads a file and prints. It touches no repository, opens no network connection, and changes
nothing. Iterate against it until it is clean — that is the fastest way to a plan somebody will
accept, and it means what lands on the reviewer's desk is a decomposition to judge rather than a
file to debug.

For the file format, run:

```sh
dispatchkit schema
```

That is the reference, and it is whichever version is installed here. Prefer it to anything you
remember about the format, including anything implied by this document.

**You must never run `dispatchkit apply`, `dispatchkit watch`, or any other subcommand.** `apply`
creates and edits real issues, and issues are where this system keeps all of its state — there is
no database behind them. A plan is a bet about how work should be divided, and a human agreed to
review that bet before it is placed. When your graph validates cleanly, say so and
**hand the file to a human**; do not place the bet yourself.

If you were handed a *single task* to implement rather than asked to plan, this skill does not
apply to you. Do the task described in the issue, make its acceptance command pass, and open a
pull request.

---

For an agent or a person turning a piece of work into a `dispatchkit` plan. The key reference is
what `dispatchkit schema` prints; this page is the judgement that reference cannot encode.

Your output is one `*.tasks.toml` file plus one Markdown brief per task. It must pass:

```sh
dispatchkit validate --strict docs/plans/<name>.tasks.toml
```

`--strict` matters. The warnings are not nitpicks — each one names a way a plan wastes an agent.

### What you are actually deciding

A plan is a bet about parallelism. Makespan is roughly the critical path length multiplied by
(work + overhead), and dispatch overhead is fixed and far from free: a run, a branch, a pull
request, a review. So the arithmetic is blunt.

- Splitting one task into two **parallel** tasks shortens the schedule.
- Splitting one task into two **serial** tasks lengthens it by exactly one overhead and buys
  nothing.

A plan that is one long chain is the worst possible shape: it pays overhead per task and gets no
concurrency in return. If you find yourself writing one, the decomposition is wrong, not the tool.

This is also why "just split it smaller" is bad advice on its own. Smaller is only better when the
pieces are **independent**.

**What the numbers turned out to be.** `dispatchkit retro <graph>` measures all of this from a
finished plan's issue timeline, and the first two plans it was pointed at both came out *slower
than serial* — 0.9x and 0.8x. Both promised more width than they achieved (4 → 2, and 2 → 1
under `caps.local = 1`). So treat a promised width as the ceiling it is, check it against the
caps the plan will actually run under, and run `retro` on your plan when it finishes rather than
trusting the arithmetic above. Overhead is only comparable within a lane; the report explains why.

### The procedure

**1. Find the interface first.** The single highest-value move is to identify the contract that
several pieces of work depend on — the port, the schema, the type — and make it its own task that
everything else depends on. One task defines it; three implement against it in parallel. This is
how a chain becomes a fan.

**2. Write the dependencies you can justify.** Each edge needs a `for`, and it must say what the
task genuinely needs from its prerequisite:

```toml
depends = [{ on = "ports", for = "the GitHubApi signature it implements" }]
```

A reason you cannot write in a clause is usually not a dependency. The single most common cause of
a needlessly serial plan is an edge added out of vague unease — "it feels like this should come
after that." Delete those. If two tasks would genuinely conflict in the same file, that is a
decomposition problem to fix in step 1, not an edge to add here.

**3. Make each task's acceptance a real command.** One shell command that decides done, runnable
on a clean checkout, and a subset of what CI runs — where "subset" means a CI command is a literal
prefix of it, so `uv run pytest -q tests/test_widget.py` is covered by a CI step of
`uv run pytest -q`. The paths it names need not exist yet. `uv run pytest -m unit tests/test_widget.py` is
an acceptance. "The tests pass and it looks right" is not. If you cannot write the command, the
task is not defined well enough to hand to anyone.

Subset does not mean *narrow*. Every CI step the task's work could fail belongs in the acceptance,
formatters included. An acceptance that omits one does not make the task easier to pass — it makes
its failure unactionable: the agent passes its own definition of done, CI fails on something it was
never asked to run, and `verify: auto` will not merge a red pull request. Nothing lints this for
you; a subset is exactly what the checker checks for.

**4. Declare `touches` generously.** The globs the task is expected to change, *including the test
files its acceptance needs*. It is advisory — it improves scheduling and produces a drift report,
and it does not gate the merge — so over-declaring costs a little parallelism and under-declaring
costs a wrong signal. Err wide.

**5. Write the brief.** Every task gets a `body_file`. Without one, the issue body is the task's
title, and that prose *is* the agent's prompt: it is told what to call the work but not what it
is.

**6. Run `validate --strict` and act on what it says.** Not by suppressing it — by changing the
decomposition. See the table below.

### The brief

Five short sections, and no more:

1. **The goal**, as an outcome rather than a sequence of steps. You are briefing something that
   can decide how; tell it what and why.
2. **What it unblocks.** This is what tells an agent how much generality to build — a port three
   tasks depend on needs more care than a leaf.
3. **The non-obvious constraints only.** This is where a decision made once, somewhere else, gets
   localised to the task that needs it. Obvious constraints are noise.
4. **The definition of done**, in prose. The command is on the issue already; this says what the
   command is standing in for.
5. **The context pointer.** Usually the plan's top-level `doc`, which every issue carries already.
   If you name a specific file here, add it to `touches` — `scope-omits-named-path` will otherwise
   tell you the brief and the declaration disagree, and widening `touches` is the right fix.

It must not restate the plan, must not give step-by-step instructions, and must not repeat
`depends`, `touches` or `verify`. Those are on the issue in structured form; a prose copy is a
copy that drifts, and the lints will catch you.

Step-by-step instructions are the tempting mistake. They convert an agent into an interpreter,
which is the one thing it is worse at than a shell script, and they encode your guess about the
implementation into a brief that then cannot adapt when the guess is wrong.

### Choosing `verify`

`verify = "auto"` merges the pull request itself when CI is green. Use it when the acceptance
command genuinely decides the question — a pure function, a parser, a well-tested adapter.

`verify = "human"` when correctness is not fully captured by a test: anything touching security,
credentials, public interfaces, deletion, or the shape of something others will build on. Choosing
`human` costs a review; choosing `auto` wrongly costs a merged mistake.

The default is `human`. Do not promote a task to `auto` to make the plan look faster.

### What the warnings mean

| Warning | What to change |
| ------- | -------------- |
| `no-decomposition` | One task is not a plan. Either find the parallelism or do the work directly. |
| `chain-graph` | Nothing runs concurrently. Go back to step 1 and find the shared interface. |
| `mostly-serial` | The critical path is nearly the whole plan. Some edge is probably unjustified — check every `for`. |
| `merge-candidate` | Two tasks are serial, routed identically, and nothing else depends on the first. They are one task paying two overheads. |
| `scope-omits-tests` | The acceptance runs tests but `touches` names no test path. If the task writes the test it is judged by, that path is in scope. |
| `thin-body` | A task with no brief. Write one. |
| `scope-omits-named-path` | The brief names a file `touches` does not cover. One of the two is wrong. |

### A worked shape

The bad version, and the reason it is bad:

```toml
# Chain: four overheads, zero concurrency.
# parse -> validate -> render -> document
```

The same work, decomposed around its interface:

```toml
# model      (the types everything shares)
#   |-- parse      \
#   |-- validate    >  three tasks, dispatched together
#   `-- render     /
#         |
#       document   (depends on all three, because it describes them)
```

Depth 3 instead of 4, width 3 instead of 1. Same total work, and the middle rank runs at once.

### Before you hand it over

- `dispatchkit validate --strict` passes.
- Every task has a `body_file`, and every brief is five sections or fewer.
- Every `depends` edge has a `for` you would defend out loud.
- Every `acceptance` is a command you could paste into a terminal.
- Every `verify = "auto"` is one you would be comfortable never reading.

<!-- written by dispatchkit 0.4.0 -->
