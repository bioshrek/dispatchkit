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

