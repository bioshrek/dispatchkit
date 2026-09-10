# The plan schema

A plan is one `*.tasks.toml` file. It is the only thing a human reviews, and everything
downstream — the issues, the dispatch order, the merges — is derived from it. This page is the
reference for what may appear in it and what each key commits you to.

The schema is **closed**: an unknown key is an error, not a shrug. A misspelled `touchs` that
parsed as nothing would silently widen a task's scope, and the failure would appear later, as a
merge conflict, far from its cause.

Check a plan before you push it:

```sh
dispatchkit validate docs/plans/thing.tasks.toml          # errors and warnings
dispatchkit validate --strict docs/plans/thing.tasks.toml # warnings are failures
```

## Authority and forecast

The keys divide in two, and the difference decides how much weight each may carry.

**Authority keys** record a decision. `lane`, `verify`, `milestone`, `spend`, `requires` are true
the moment they are written, because writing them is what makes them true. When you set
`verify = "human"` you are not predicting that review will be needed; you are requiring it.

**Forecast keys** are claims about work that has not happened yet. `touches` and `depends`
describe an implementation nobody has written. They are useful, and they are guesses.

Forecasts drift. That is not a defect in the author, it is what a forecast is. So a forecast may
inform scheduling and may be reported on, but it never holds a veto — see the D9.2 record in
[design.md](design.md). The practical consequence is in `touches` below.

## Top-level keys

| Key    | Required | Type            | Meaning                                          |
| ------ | -------- | --------------- | ------------------------------------------------ |
| `plan` | no       | string          | Plan name. Defaults to the filename stem. Becomes the `plan:<name>` label and the machine block's `plan`, so it is a wire format — renaming a live plan orphans its issues. |
| `task` | yes      | array of tables | The tasks. At least one.                         |
| `doc`  | no       | string          | A document the agent should read as background.  |
| `base` | no       | string          | The branch this plan's tasks integrate on. Defaults to `main`. |

`base` is how a plan becomes a feature branch rather than a stream of increments landing in the
trunk. Every task in the plan branches from it and targets it, in both lanes, and the finished
plan is merged into the default branch by a human — once, deliberately. Omit it and the plan
behaves exactly as it always has. It must be a valid git branch name; a leading `-` is refused
outright, because it would read as an option rather than a ref.

`doc` is prose for a language model, not a machine key. It renders one line into every issue body
of the plan, and that line carries its own precedence: **the issue is the contract, the document
is context, and a disagreement between them is reported in the pull request rather than
reconciled by the agent.** The ranking is rendered into the body rather than into a prompt
template because the cloud lane has no template, and a rule only one lane is told is not a rule.

The reference is not pinned to a commit. A plan edited between review and run therefore gives the
agent a brief nobody approved — accepted, because the part that binds is on the issue.

## Task keys

### Required

| Key          | Type   | Meaning                                                        |
| ------------ | ------ | -------------------------------------------------------------- |
| `id`         | string | Stable identity, a lowercase kebab slug, `^[a-z0-9]+(?:-[a-z0-9]+)*$`. Written into the machine block and used to match a task to its issue on every later pass, so **renaming an id after the push orphans the issue** and creates a second one. |
| `title`      | string | The issue title. |
| `milestone`  | string | Groups tasks for reporting. Free text. |
| `lane`       | enum   | `cloud` or `local`. Where the work runs. |
| `acceptance` | string | One shell command that decides done. |

`acceptance` is injected verbatim into the issue body, so the agent, the local runner and the
reviewer all run the same check. Two rules follow from that:

- It must be a **subset of what CI runs**, or `verify = "auto"` merges on a green check that never
  ran the thing the task promised. `validate` refuses the graph (`acceptance-not-in-ci`) rather
  than warning, because the failure mode is a silent merge.

  "Subset" is **prefix matching on the literal command string**: a CI command must equal the
  acceptance clause or be a prefix of it. So with `uv run pytest -q` in CI, an acceptance of
  `uv run pytest -q tests/test_widget.py` passes — it narrows what CI runs — while
  `uv run pytest tests/test_widget.py` does not, because it is not an extension of any CI command.
  An acceptance of several `&&`-joined clauses must have every clause covered. The check is a
  conservative approximation and it is wrong in the safe direction: it can refuse a task CI really
  does cover, and the remedy is to widen CI or use `verify = "human"`. Neither merges by mistake.
  The check only applies to `verify = "auto"` tasks.
- It must be **runnable on a clean checkout**. `uv run pytest -m unit -k widget` is an acceptance;
  "the tests pass and it looks right" is not.

### Optional

| Key         | Type            | Default   | Meaning                                        |
| ----------- | --------------- | --------- | ---------------------------------------------- |
| `verify`    | enum            | `human`   | `auto` merges the pull request itself on green CI. `human` waits for a person. |
| `spend`     | boolean         | `false`   | `true` withholds dispatch until a human adds the `spend:approved` label. |
| `requires`  | list of strings | `[]`      | Capabilities the runner must have; any value makes the task local-only. |
| `touches`   | list of strings | `[]`      | Glob paths the task is expected to change. |
| `depends`   | array of tables | `[]`      | Prerequisites. See below. |
| `body_file` | string          | none      | Path to the brief, resolved **relative to the plan file**. |

`spend` is a gate, not a routing decision: **either** lane refuses to dispatch a `spend = true`
task until the label appears. Conflating "this is expensive" with "this runs over there" would
lose the ability to say an expensive task must still run locally.

`requires` names capabilities the runner must have, and a task that requires anything is not cloud
eligible — the cloud lane has no way to prove it has a GPU or a database. A capability the
configuration does not define is an error, not a warning: dispatching a task to a runner that
cannot do it wastes an attempt.

#### `depends`

```toml
depends = [{ on = "ports", for = "the GitHubApi signature" }]
```

Both keys are required. `on` is another task's `id`; `for` says what this task actually needs from
it, and it is not decoration — a reason you cannot write is usually a dependency you do not have,
and the most common cause of a needlessly serial plan is an edge added out of vague unease.

A dependency is a **hard ordering constraint**: the task is not dispatched until `on` is closed.
Cycles, self-edges, duplicates and unknown targets are all errors.

#### `touches`

An **advisory** declaration of the files the task is expected to change, matched as `fnmatch`
globs, so `*`, `?` and `[...]` all work. Note that `fnmatch` is **not** path-aware: `*` crosses
directory separators, so `docs/*.md` already matches `docs/plans/a.md`, and writing `docs/**/*.md`
narrows rather than widens — it requires at least one intermediate directory and so misses
`docs/a.md`. Prefer the single star. Paths need not exist yet; the plan describes work nobody has
done. `touches` does two things:

- The scheduler prefers not to run two tasks with overlapping scope at the same time, which
  reduces conflicts.
- After the fact, a pull request that changed files outside its declared scope produces a drift
  notice, which is a signal about the plan.

It does **not** gate the merge, and it is not a permission boundary. It is per-task, predicted,
and parsed out of an issue body that anything with write access can edit — so it can be widened by
the very thing it would be constraining. The real boundary is the repository-level path fence in
`.github/dispatchkit.toml`, which is static and cannot be widened from an issue.

Declare `touches` **generously**. Under-declaring costs a weaker exclusion and a notice;
over-declaring costs a little scheduling parallelism. Include the test files the task's
`acceptance` needs — if the task writes the test it is judged by, that path is in scope, and
`scope-omits-tests` will say so.

#### `body_file`

The brief. Without it the issue body is the task's title and its acceptance command, and that
prose *is* the agent's prompt — so the agent is told what to call the work but not what it is.
`validate` warns (`thin-body`).

Five short sections, and no more:

1. **The goal**, as an outcome rather than a sequence of steps.
2. **What it unblocks** — this is what tells an agent how much generality to build.
3. **The non-obvious constraints only.** This is where a decision made once, elsewhere, gets
   localised to the task that needs it.
4. **The definition of done.**
5. **The context pointer.** Prefer the plan's top-level `doc` key, which every issue already
   carries. If a brief does name a specific file, make sure `touches` covers it — that is exactly
   what `scope-omits-named-path` is checking, and the answer is almost always to widen `touches`
   rather than to remove the reference.

It must not restate the plan, must not give step-by-step instructions, and must not repeat
`depends`, `touches` or `verify`. A prose copy of structured data is a copy that drifts, and
`scope-omits-named-path` catches one direction of that drift: a body naming a path the task's
`touches` does not cover.

## A minimal plan

```toml
plan = "widget"
doc = "docs/design.md"

[[task]]
id = "ports"
title = "Define the widget port"
milestone = "M1"
lane = "cloud"
acceptance = "uv run pytest -m unit tests/test_ports.py"
touches = ["src/widget/ports.py", "tests/test_ports.py"]
body_file = "widget/ports.md"

[[task]]
id = "http-adapter"
title = "Implement the HTTP adapter"
milestone = "M1"
lane = "cloud"
verify = "auto"
acceptance = "uv run pytest -m unit tests/test_http.py"
touches = ["src/widget/http.py", "tests/test_http.py"]
body_file = "widget/http.md"
depends = [{ on = "ports", for = "the port signature it implements" }]

[[task]]
id = "memory-adapter"
title = "Implement the in-memory adapter"
milestone = "M1"
lane = "cloud"
verify = "auto"
acceptance = "uv run pytest -m unit tests/test_memory.py"
touches = ["src/widget/memory.py", "tests/test_memory.py"]
body_file = "widget/memory.md"
depends = [{ on = "ports", for = "the port signature it implements" }]
```

The two adapters depend on the port and not on each other, so they dispatch together. That shape
is the point of the whole exercise: a plan whose tasks form a single chain pays dispatch overhead
per task and gets no concurrency for it, and `validate` says so (`chain-graph`).

## What `validate` reports

**Errors** stop the push:

`acceptance-not-in-ci`, `cycle`, `dangling-dependency`, `duplicate-capability`, `duplicate-dependency`, `duplicate-id`, `empty-acceptance`, `empty-edge-reason`, `empty-field`, `empty-graph`, `invalid-enum`, `invalid-id`, `invalid-toml`, `invalid-type`, `invalid-value`, `lane-capability-mismatch`, `missing-key`, `self-dependency`, `unknown-capability`, `unknown-key`.

<!-- warnings -->

**Warnings** report on the shape of the decomposition and never stop anything unless you pass
`--strict`:

`no-decomposition`, `chain-graph`, `mostly-serial`, `merge-candidate`, `scope-omits-tests`,
`thin-body`, `scope-omits-named-path`.

The split is deliberate. An error means the plan cannot be executed as written. A warning means it
can, but a human should look — and only a human can decide whether a serial plan is bad
decomposition or a genuinely serial problem.
