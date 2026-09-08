# Context

Agentic coding is really good at doing tasks with clear boundaries. So the daily routine of a developer becomes more and more like project management, where tasks are broken into smaller sub-tasks, and these sub-tasks are dispatched, tracked, reviewed and completed manually.

To make it more efficient, an automated pipeline can be established to handle the dispatch, tracking, and completion of these sub-tasks, reducing the manual overhead and improving overall productivity.

## Current GitHub Utilities

- **GitHub Actions**: Automates workflows for CI/CD, testing, and deployment.
- **GitHub Projects**: Provides project management features like task boards, issue tracking, and automation for organizing and prioritizing work.
- **GitHub Issues**: Allows for tracking tasks, bugs, and feature requests, with support for labels, milestones, and assignees to manage workflow efficiently.

## What we need

- A skill to create GitHub Issues for the sub tasks defined in a plan, and manage their dependencies through GitHub Projects.
- A dispatcher that could assign dependency-resolved sub-tasks to the appropriate coding agents automatically: cloud agents, local agents.

---

# Technical Design

## Overview

Three pieces, each with a single responsibility:

| Piece         | Where                                                | Responsibility                                                                             |
| ------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| **Planner**   | `.agents/skills/task-dispatch/SKILL.md`              | Plan markdown → task graph file → GitHub Issues + Project items (one-shot, human-reviewed) |
| **Scheduler** | `src/dispatchkit/` + `.github/workflows/dispatch.yml` | Recompute the ready set from issue state; assign ready tasks to an agent lane              |
| **Runners**   | Copilot coding agent (cloud) / local daemon          | Execute one issue, open a PR that closes it                                                |

GitHub is the only state store — no external DB. Issues hold the task, the Project holds
the derived scheduling state, and everything the scheduler needs is recomputable from them.

```mermaid
flowchart LR
    P[plan.md] -->|Planner skill| G[tasks.toml]
    G -->|apply| I[GitHub Issues + Project]
    I --> S{Scheduler}
    S -->|lane: cloud| C[Copilot coding agent]
    S -->|lane: local| L[local agent daemon]
    C --> PR[Pull Request]
    L --> PR
    PR -->|verify: auto, CI green| M[auto-merge]
    PR -->|verify: human| R[review]
    M -->|closes #N| I
    R -->|closes #N| I
```

## Planner: deriving the graph

Decomposition quality decides everything downstream — an over-split plan drowns in per-task
overhead, an under-split one runs serially and buys nothing. Neither is fixed by asking a model
for "the right number of subtasks", so the planner is grounded in rules that are checkable
instead.

### From requirements to milestones

**No acceptance criterion, no plan.** A requirement is only plannable once it can be stated as a
command with a binary exit code. If that command can't be written yet, the requirement goes back
for sharpening rather than into a graph. This is the same discipline as
[implementation_plan.md](implementation_plan.md)'s "every milestone ends in one command with a
binary exit code", and it is what later makes `verify: auto` possible at all.

From there:

- **A milestone is one acceptance criterion flipping red to green.** Each milestone names the
  AC(s) it turns. A milestone that turns none is scaffolding — permitted, but it must say which
  later AC it unblocks.
- **Verifiers land before the code they guard**, and are observed failing first. A gate that has
  never been seen to fail proves nothing, and auto-merge is only safe on gates that have.
- Existing plans are the calibration fixtures. [refactor_requirements.md](refactor_requirements.md)
  (R1–R8, AC1–AC10) and its M0–M9 execution plan are fed to the planner as worked examples, not
  described in prose.

### Milestone is not task

A **milestone is a verification checkpoint**; a **task is a dispatch unit**. Keeping them as two
layers is what allows a legible proof narrative _and_ a wide dispatch graph at the same time —
one milestone may fan out into five parallel tasks, and three tiny serial milestones may collapse
into one task. Conflating them forces a choice between the two.

### When a task boundary is justified

Tasks are the unit of routing and verification, not of work. A boundary between two adjacent
pieces of work must earn itself on at least one of:

1. **Parallelism** — they can run concurrently, widening the graph
2. **Routing** — different `requires`/lane, so a different runner is needed
3. **Verification** — different `verify` or `spend`, so a different gate is needed
4. **Size** — merged, they would not fit one cloud agent session
5. **Revertability** — they should be independently revertable

If none applies, merge them. The cost model behind the rule is blunt: makespan is roughly
critical-path length × (work + overhead), where overhead — issue, agent boot, clone, install, CI,
merge queue — is fixed and far from free. Splitting one node into two **parallel** nodes shortens
the schedule; splitting it into two **serial** nodes lengthens it by exactly one overhead and buys
nothing. Hence the flat rule: **never split a serial chain.**

### Manufacturing parallelism

Width is designed, not discovered. The reliable move is **interface-first**: land the shared
contract as one small task, then fan out implementations that depend on it and not on each other.
The layering already gives the seams — `application/ports.py` first, then every adapter in
`infrastructure/adapters/` in parallel with disjoint `touches`.

The counterpart is the `for` field on every dependency edge. Ordering things by narrative
plausibility rather than data dependency is the most common decomposition error, and it silently
serialises the graph. Requiring each edge to name the artifact, interface, or file it waits on
makes spurious edges visible; every one deleted widens the graph.

### Structural lints

`validate` computes and reports, so the review gate is quantitative rather than a vibe check:

| Signal                                         | Meaning                                |
| ---------------------------------------------- | -------------------------------------- |
| `count == 1`                                   | No decomposition happened              |
| `width == 1` (max antichain)                   | A chain wearing a graph's clothes      |
| `depth` close to `count`                       | Mostly serial; look for spurious edges |
| Serial pair, no other dependents, same routing | Merge candidate                        |
| Estimated work below ~3× overhead              | Task is under the economic floor       |

These are warnings, not hard errors — the human ratifies the graph anyway, and the point is to
make that ratification informed. Alongside them the planner emits the plan's shape:
`12 tasks, depth 4, width 5, 3 human-verify, est. 2 review sessions`.

### Calibration from outcomes

Dispatch history is the feedback loop, and the two failure modes have distinct signatures:

| Too coarse                      | Too fine                          |
| ------------------------------- | --------------------------------- |
| Tasks hit the agent session cap | Overhead dominates wall-clock     |
| High `Attempts`, large diffs    | Many trivial PRs                  |
| Scope drift against `touches`   | High depth, low width             |
| —                               | Merge-queue churn on shared files |

"Share of tasks hitting the session cap" is the single most useful dial: it measures the ceiling
directly. Once enough history exists, a calibration summary is folded back into the planner skill
so later plans are sized against observed behaviour rather than a guess.

None of this makes the planner correct. It makes it auditable and self-correcting — which is the
achievable bar, and the reason the human ratification gate stays.

## Task graph format

The planner's intermediate artifact, committed under `docs/plans/<plan>.tasks.toml`. It is the
reviewable unit: a human edits this, not the issues.

```toml
[[task]]
id        = "m5a-values"          # stable slug, unique per plan
title     = "Value objects for ClipId/Duration"
milestone = "M5a"                 # the verification checkpoint this serves
lane      = "cloud"               # cloud | local — planner proposes, human ratifies
requires  = []                    # capability tags the runner must provide
verify    = "auto"                # auto | human — governs merge, default human
spend     = false                 # true → needs `spend:approved` before dispatch
touches   = ["src/podkit/domain/**"]     # advisory file scope — see conflicts
acceptance = "uv run pytest -m unit && uv run lint-imports"
body_file = "docs/plans/m5a-values.md"   # optional long-form spec

# every edge names the artifact it waits on — unjustifiable edges get deleted
depends = [{ on = "m2-cassette", for = "CassetteStore.get() signature" }]
```

Invariants checked before anything is pushed: unique ids, every `depends` resolves, the graph
is acyclic (Kahn's algorithm; a cycle is a hard error naming the cycle), every edge carries a
non-empty `for`, and `acceptance` is non-empty. `id` is the idempotency key — re-running `apply`
updates the existing issue rather than creating a second one.

`lane` and `verify` are both **planner proposals**. The planner emits its best guess with a
one-line rationale in a `# why:` comment; neither takes effect until a human has reviewed and
merged the graph file. This is the single human gate in the whole pipeline, and it is placed
here deliberately — reviewing one table of routing decisions up front is cheap, whereas
reviewing every PR afterwards is the bottleneck we are trying to remove. The `requires` tags
make that review checkable rather than a matter of opinion: the validator rejects
`lane = "cloud"` on any task whose `requires` the cloud lane cannot satisfy.

## GitHub representation

- **Issue** — one per task. Body ends with a fenced machine block so the scheduler never has to
  parse prose:

  ```yaml
  <!-- dispatchkit
  v: 1
  id: m5a-values
  plan: refactor
  milestone: M5a
  lane: cloud
  requires: []
  verify: auto
  spend: false
  depends: [m2-cassette]
  touches: ["src/podkit/domain/**"]
  -->
  ```

  Labels mirror it for cheap filtering: `dispatchkit`, `plan:refactor`, `lane:cloud`,
  `verify:auto`.

- **Project (v2)** — a **single board for all plans**, custom fields:
  - `Status`: `Blocked` → `Ready` → `Dispatched` → (`In Review` | `Auto-merging`) → `Done`
  - `Lane`: `cloud` | `local`
  - `Verify`: `auto` | `human`
  - `Task ID`: text, mirrors the slug
  - `Attempts`: number, incremented on each dispatch

  The Project is a **derived view**. If it is deleted, the scheduler can rebuild it from the
  issues alone; state lives in issue open/closed + the machine block. One board keeps the
  scheduler to a single query, defines the fields once, and lets concurrency caps bound total
  in-flight work across every plan at once; `plan:*` labels drive per-plan filtered views.

- **Dependency edges** live only in the machine block. GitHub's native "blocked by" links are
  intentionally not used: they are not exposed uniformly across REST/GraphQL and cannot be
  round-tripped from a committed file.

## Scheduler

A single idempotent pass, safe to run repeatedly:

1. **Load** — one GraphQL query pulls all `label:dispatchkit` issues with state, assignees, linked
   PRs, and Project field values.
2. **Resolve** — a task is _ready_ iff it is open, unassigned, and every id in `depends` maps to
   a closed issue. Everything else is `Blocked`.
3. **Reconcile** — write back `Status` for every item so the board is always truthful, not just
   for the ones being dispatched.
4. **Admit** — take ready tasks in plan order, subject to per-lane concurrency caps
   (`cloud: 3`, `local: 1` by default, configurable in `.github/dispatchkit.toml`) and the file-scope
   exclusion below. Caps are what keep review load bounded and PRs from stacking into conflict.
5. **Dispatch** — per lane, below.

Dispatch is the only mutating step, and it is guarded by assignment: assigning the issue _is_
the lock. Two concurrent scheduler runs converge because step 2 re-reads assignees, and a
double-assign attempt is a no-op.

### Lane routing

Lanes differ by **runner capability**, not by subject matter or by cost. A cloud coding agent is
an ephemeral, sandboxed, time-boxed container; the local lane is a real workstation. That is the
whole distinction:

| Constraint         | cloud (hosted coding agent)        | local (dev workstation)                |
| ------------------ | ---------------------------------- | -------------------------------------- |
| Session wall-clock | hard cap around an hour            | unbounded                              |
| OS                 | ephemeral Linux container          | whatever the workstation runs          |
| Network            | firewall allowlist, no free egress | unrestricted                           |
| Filesystem         | fresh clone, no host state         | full tree, caches, large local data    |
| Hardware           | no GPU, no peripherals             | GPU, audio/video devices, peripherals  |
| Interactivity      | none                               | can drive a UI or a human-in-the-loop  |
| Secrets            | only what the platform injects     | the workstation's own credential store |

A task declares what it needs as `requires` tags — `long-run`, `os:macos`, `os:windows`, `gpu`,
`device`, `net:unrestricted`, `local-data`, `interactive`. The routing rule is one line:

> A task may run in the cloud lane iff its `requires` is empty. Any tag that no cloud runner
> advertises forces `local`.

Stating it as capabilities rather than as a hand-maintained list of "local-ish work" is what
keeps this general. Adding a self-hosted runner later means advertising `gpu` or `os:macos` on a
new lane and changing no task definitions; the routing rule already accommodates it.

Dispatch per lane:

- **cloud** — assign the issue to the coding agent via the GraphQL `replaceActorsForAssignable`
  mutation. The agent opens a draft PR; CI is the gate.
- **local** — label the issue `dispatch:local` and stop; a daemon on the workstation picks it up
  (see [Local daemon](#local-daemon)).

### Spend is orthogonal to lane

Cost is a separate axis. A cloud task can burn paid API credits and a local task can be free, so
`spend` is its own flag rather than a reason to route somewhere. When `spend = true`, **either**
lane refuses to dispatch until a human adds the `spend:approved` label. Conflating the two would
mean either that paid work is stuck on one machine or that free work inherits an approval gate
it doesn't need.

### Triggers

`.github/workflows/dispatch.yml`:

```yaml
on:
  issues: { types: [closed, reopened, labeled] }
  pull_request: { types: [closed] }
  schedule: [{ cron: "7,37 * * * *" }] # safety net; offset off the busy :00/:30
  workflow_dispatch:
```

Event-driven for latency, cron for self-healing. Because the pass is idempotent, an extra run
costs one API round trip and changes nothing.

## Parallel work and file conflicts

Non-overlapping file scope is **not** a hard requirement, and trying to make it one would be a
mistake. File sets can't be known accurately before an agent starts work, so a declared scope is
either over-broad (serialising everything and destroying the parallelism we're buying) or
under-broad (giving false confidence). Worse, the granularity is wrong in both directions: two
tasks can edit different functions in one file and merge cleanly, while two tasks in entirely
separate files can still conflict semantically when one renames a symbol the other calls.

So conflicts are handled in four layers, weakest and cheapest first:

1. **Decomposition (primary).** `depends` is the real conflict-avoidance lever, not just logical
   ordering. If two tasks would rewrite the same module, the planner should chain them instead of
   letting them race. Reviewing that is part of the graph review gate.
2. **Advisory exclusion (scheduling hint).** `touches` holds glob patterns. Admission skips a
   ready task whose `touches` intersects that of any already-`Dispatched` task; it waits for the
   next pass. Cheap, and it degrades gracefully — worst case is extra serialisation, never a bad
   merge. Omitting `touches` means "unknown scope", which excludes nothing.
3. **Merge queue (authoritative).** Required-up-to-date branch protection plus a GitHub merge
   queue means every PR is rebased onto the tip and re-tested in order before it lands. A textual
   conflict becomes a mechanical failure; a semantic conflict becomes a red test. This is the
   layer that actually guarantees correctness, and it is why layer 2 can stay advisory.
4. **Scope drift check.** After a PR opens, the scheduler diffs its changed files against the
   declared `touches`. Drift is reported on the issue, and for `verify = "auto"` tasks it
   withholds auto-merge — an agent that wandered outside its declared blast radius is exactly the
   case a human should look at. Over time this also tells us which planner decompositions were
   wrong.

A PR that can't be rebased cleanly is returned to the agent as a normal failure: comment,
`Attempts += 1`, and after the retry budget it lands in `dispatch:stuck` for a human.

## Local daemon

The scheduler runs in Actions, but the `local` lane needs a long-lived process on the machine
that provides the capabilities the cloud sandbox lacks. `src/dispatchkit/daemon.py`:

**Lifecycle.** Start as a foreground `make dispatch-daemon` while the loop is still being
trusted — it is observable and trivially killable. Promote to a macOS `launchd` user agent
(`~/Library/LaunchAgents/com.artstrategy.dispatchkit.plist`, `RunAtLoad` + `KeepAlive`, logs to
`~/Library/Logs/dispatchkit/`) only once it has run unattended without surprises. A `flock` on
`~/.dispatchkit/daemon.lock` enforces one instance, so a stray terminal can't double-run it.

**Claiming.** GitHub is the only shared state, and assignment is not compare-and-swap, so the
daemon assigns itself and then **re-reads** the issue to confirm it actually won; on a lost race
it drops the task and moves on.

**Isolation.** Each task runs in its own `git worktree` under `~/.dispatchkit/work/<task-id>` on a
fresh branch from `origin/main`, removed on success and retained on failure for inspection.
Default concurrency is 1, which keeps resource contention and log interleaving manageable.

### Liveness: how GitHub can monitor a process it cannot see

There is no GitHub feature that health-checks an external process — no inbound ping, no agent
registration, nothing that reaches your workstation. GitHub is **passive storage**. So liveness
is built out of two active parties writing to and reading from it:

1. **The daemon writes a heartbeat.** Every 5 minutes it overwrites a timestamped record on
   GitHub. Concretely: a single pinned issue titled `dispatchkit: daemon status`, holding one
   comment per host, edited in place via `updateIssueComment` so it never grows. The comment body
   is a small JSON block — `host`, `pid`, `version`, `last_seen`, `current_task`. Editing one
   comment is a single API call, is human-readable in the browser, and needs no Project field
   plumbing. (A Project `Last Seen` date field is the alternative, but it only describes a
   claimed item, so it can't tell you an _idle_ daemon is alive.)
2. **The scheduler reads it and judges.** The cron pass already runs every 30 minutes. It
   compares `now - last_seen` for each host and treats anything past 30 minutes as dead: tasks
   claimed by that host are unassigned, `Attempts` is incremented, and they return to `Ready`.

That is the entire mechanism — a **dead man's switch**. The daemon proves it is alive by
repeatedly saying so; silence is the failure signal. Nothing pushes toward your laptop, and the
daemon is never asked a question it might be too dead to answer. The scheduler, not GitHub, is
the monitor; GitHub is just the mailbox they share.

One caveat worth designing around: `schedule:` triggers are best-effort — they can be delayed
under load, and GitHub disables them entirely after 60 days of repository inactivity. A silently
disabled cron is a monitor that has itself died, so the alerting path below treats "no scheduler
run in 24h" as its own alertable condition rather than assuming the cron is running.

**Observability.** Each run appends to a local JSONL log; on completion the daemon posts the tail
to the issue, so the trace lives on GitHub rather than only on one workstation. A
`dispatch:pause` repo label, checked every poll, stops both the daemon and the cloud scheduler
without anyone having to find the process.

## Alerting

Both halves of the system fail in ways that are invisible until someone opens the board, so
failures are pushed to a Feishu/Lark group via a **custom bot** webhook.
Reference: [Custom bot usage guide][lark-bot].

[lark-bot]: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot

**Transport.** `POST` JSON to `https://open.feishu.cn/open-apis/bot/v2/hook/<token>`. Two message
shapes cover everything we need:

```jsonc
// routine alert
{ "msg_type": "text", "content": { "text": "dispatchkit alert: #123 retries exhausted" } }

// alert with a jump link to the issue
{ "msg_type": "interactive", "card": { /* button with an open_url behavior */ } }
```

The scheduler reads `LARK_WEBHOOK_URL` from Actions secrets; the daemon reads it from the
workstation credential store.

**Configuration.** The webhook URL is `https://open.feishu.cn/open-apis/bot/v2/hook/<token>`,
where the token alone is enough to post to the group. It is therefore never committed — not to
this document, not to `dispatchkit.toml`, not to a `.env` that could be staged. Both halves read it
by name:

```sh
# cloud scheduler
gh secret set LARK_WEBHOOK_URL      # paste at the prompt, never as an argv
gh secret set LARK_WEBHOOK_SECRET   # signing secret from the bot's security settings

# local daemon (macOS keychain)
security add-generic-password -a "$USER" -s dispatchkit-lark-webhook -w
security add-generic-password -a "$USER" -s dispatchkit-lark-secret  -w
```

Piping the value in as a command argument would leave it in shell history and in process
listings, so both forms above read from a prompt instead.

**Security setting: signature, not IP allowlist.** The bot offers custom keywords, an IP
allowlist, and signature verification. The allowlist is a non-starter here — it caps at 10
entries while hosted Actions runners egress from a large, rotating address range, and a
workstation is usually behind a dynamic address too. So we enable **signature verification**:
sign with `timestamp + "\n" + secret` as the HMAC-SHA256 _key_ over an empty message, base64 the
digest, and send `timestamp` and `sign` alongside `msg_type`. The timestamp must be within an
hour, so a workstation with a badly skewed clock fails with `19021` — worth recognising rather
than debugging as a network problem.

**Rate and size limits shape the design.** A bot is capped at 100 messages/minute and 5/second,
the body must stay under 20 KB, and the platform explicitly warns that sending on the hour or
half hour risks `11232` throttling under load. Two consequences:

- The cron trigger is offset to `7,37 * * * *` rather than `0,30`, so scheduled alerts don't pile
  onto the platform's busiest moments.
- Alerts carry issue numbers and error classes, never log dumps. The 20 KB ceiling agrees with
  the security argument for the same rule.

**Check the body, not just the status.** A rejected request still returns HTTP 200 with a
non-zero `code` (`19021` bad signature, `19022` IP blocked, `19024` keyword missing, `11232`
throttled, `9499` malformed body). Treating HTTP 200 as success would silently swallow every
misconfiguration, so the client asserts `code == 0`.

**Who alerts about what.** The two sides cover each other's blind spots:

| Condition                            | Detected by | Why that side                        |
| ------------------------------------ | ----------- | ------------------------------------ |
| Task failed / retries exhausted      | scheduler   | It owns `Attempts`                   |
| Daemon heartbeat stale               | scheduler   | A dead daemon cannot report itself   |
| Agent produced no PR in time         | scheduler   | Only visible from repo state         |
| Task crashed, tooling broken locally | daemon      | It has the stack trace and the log   |
| Scheduler run missing for 24h        | daemon      | A disabled cron cannot report itself |

That last row is the point: a monitor that only runs inside the thing it monitors has no way to
report its own death, so each side watches for the other's silence.

**Not a spam cannon.** The scheduler pass is idempotent and runs every 30 minutes, so naive
alerting would re-send the same failure indefinitely. Alerts fire on **state transition only**,
recorded by an `alert:sent` label on the issue that is removed when the condition clears. A
per-run cap in `dispatchkit.toml` bounds the worst case, well under the 100/minute ceiling.

**No acknowledge button.** A custom bot's cards support only `open_url` jumps — they cannot post
back to a server, and the bot cannot read replies or recall its own messages. So an alert links
to the issue and acknowledgement happens on GitHub. Anything richer would mean building a full
bot application, which is not worth it for an alert channel.

**Best-effort.** Delivery runs with a short timeout and never fails a dispatch pass. An alerting
outage must not become a pipeline outage.

## Completion and unblocking

A task is done when its PR merges with `Closes #N`, which closes the issue, which makes
dependents ready on the next pass. There is no separate "mark complete" step to forget.

The `acceptance` command from the task graph is injected into the issue body as the definition
of done, so both agent lanes run the same check the reviewer will run. A PR that fails CI leaves
the issue open and assigned — it stays out of the ready set until a human intervenes, which is
the intended failure mode.

## Merge policy

Human review on every PR would cap the pipeline's throughput at the reviewer's attention, which
defeats the point. So merge authority is per-task, decided by `verify`:

- **`verify = "auto"`** — the task is fully machine-verifiable. Once the PR is non-draft and all
  required checks are green, the scheduler enables GitHub auto-merge (squash). The issue closes,
  dependents unblock, no human touches it.
- **`verify = "human"`** — the task's correctness is not expressible as a check: prompt/voice
  quality, API design, anything judged by taste or by listening to output. The scheduler requests
  a reviewer, sets `Status: In Review`, and stops. It never merges.

`human` is the default; `auto` must be opted into in the graph file and survive the review gate.

Three guardrails keep `auto` honest:

1. **Acceptance must be a subset of CI.** The validator rejects `verify = "auto"` unless the
   task's `acceptance` command is one the CI workflow already runs, so "green CI" literally
   means "acceptance passed" rather than "some unrelated checks passed".
2. **Blast-radius fence.** Auto-merge is refused for any PR touching a path in `fence.paths`,
   which defaults to `.github/workflows/**`, the config file itself and
   `<paths.plans>/*.tasks.toml` — the pipeline may not rewrite its own rules, its own routing, or
   its own merge permissions unattended. It is configuration, and it is derived from the settings
   it protects, so moving the plans directory moves the fence with it.
3. **Branch protection is the real enforcement.** The scheduler's token has no admin bypass, so
   auto-merge can only ever fire on a genuinely green, protected branch. A misconfigured
   scheduler fails closed.

Any task whose acceptance can only be stated in prose is a signal the task was decomposed too
coarsely — splitting it until the machine-checkable part can be `auto` is what actually grows
throughput over time.

## Failure handling

| Failure                           | Behavior                                                       |
| --------------------------------- | -------------------------------------------------------------- |
| Agent opens no PR in 24h          | Cron pass unassigns, `Attempts += 1`, returns task to `Ready`  |
| `Attempts >= 3`                   | Label `dispatch:stuck`, never auto-dispatched again            |
| Dependency issue deleted          | Dependents stay `Blocked`; validator flags the dangling id     |
| Cycle introduced by hand-edit     | `apply` refuses to push and names the cycle                    |
| Rate limit / partial write        | Next pass reconciles; no local state to go stale               |
| `auto` PR red, or fence tripped   | Auto-merge withheld, falls back to `In Review` for a human     |
| PR conflicts / fails rebase       | Merge queue ejects it; comment, `Attempts += 1`, back to agent |
| Local daemon dies mid-task        | Stale heartbeat → cron reclaims the issue after 30 min         |
| Two tasks race the same files     | `touches` exclusion defers one; merge queue catches the rest   |
| Cloud agent hits its time cap     | No PR → normal retry; repeated hits mean `requires` was wrong  |
| Scheduler cron disabled by GitHub | Daemon sees no run in 24h and alerts                           |

## Security

- Workflow runs with a least-privilege token: `issues: write`, `repository-projects: write`,
  `contents: read`. Never `contents: write` — only PRs mutate the tree.
- Issue bodies are attacker-influencable text. The machine block is parsed as YAML with a safe
  loader and validated against a strict schema; unknown keys and non-slug ids are rejected.
  Nothing from an issue body is ever interpolated into a shell command — `acceptance` is
  executed as an argv list by the runner, not through a shell.
- The local daemon holds the workstation's credentials; the cloud lane never sees them. It
  authenticates via `gh`'s keychain credential, not a PAT sitting in a file. Paid work in either
  lane additionally requires the human-applied `spend:approved` label.
- The Lark webhook URL and its signing secret are bearer credentials: Actions secrets on one
  side, credential store on the other, never logged and never echoed into an issue. A leaked
  webhook lets anyone spam the group, which is why signature verification is enabled rather than
  left off. Alert bodies carry issue numbers and error classes, not log dumps, so agent-authored
  text can't be relayed into the chat channel.

## Verification

Three separable things get verified, and only the last one needs real work dispatched:

| What                                                        | Verified by                             | Cost          |
| ----------------------------------------------------------- | --------------------------------------- | ------------- |
| **Mechanism** — graph → issues → dispatch → merge → unblock | Synthetic graphs, recorded API fixtures | Free, offline |
| **Planner judgment** — is the decomposition any good        | Diff against known-good human plans     | Free, offline |
| **End-to-end value** — does it reduce project overhead      | One real plan, actually executed        | Real          |

### Synthetic tasks, but never no-ops

A test task that appends a line to a file exercises the state machine and proves nothing. Every
interesting failure is about friction — the agent overruns its session, two PRs conflict, CI goes
red, the diff drifts outside `touches`, the issue body was ambiguous — and a no-op passes all of
them by construction. So test tasks are drawn from **real, small, independently useful chores**
that produce real diffs, real CI time and real review load.

### Verify the failures, not the successes

"A task completed" is the least informative outcome available. The acceptance list for dispatchkit is
therefore the failure table, each row provoked deliberately:

| Scenario                         | Provocation                                              |
| -------------------------------- | -------------------------------------------------------- |
| Agent never opens a PR           | Dispatch, then never produce a branch; assert reclaim    |
| Daemon dies mid-task             | `kill -9` the daemon; assert reclaim after the window    |
| Acceptance fails                 | A task whose `acceptance` exits non-zero                 |
| Scope drift                      | A task that edits a file outside its `touches`           |
| Concurrent edit of the same file | Two overlapping tasks dispatched deliberately            |
| Alert storm                      | N cron passes over one failure; assert exactly one alert |

Most of these run against recorded fixtures rather than live.

### Dogfooding, safely

Once D5 lands, D6–D9 can be dispatched by the partially built system. The obvious hazard — a buggy
dispatcher merging its own broken fix — is already closed by the blast-radius fence: anything
touching `.github/workflows/` or `dispatchkit.toml` can never auto-merge, so dispatchkit's own tasks are
`verify: human` by construction.

Live trials run in **dispatchkit's own repository**, not in a project it is managing. Failed
experiments otherwise leave permanent debris in someone's issue numbers, Project board and
`main` history. Here, debris is on-brand: the issues are the tool's own backlog, and they are
real chores on real code rather than no-ops.

## Build order

| Step | Deliverable                                            | Proven by                                                                                     |
| ---- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| D1   | Task graph schema + validator (parse, cycle, dangling) | Unit tests, no network                                                                        |
| D2   | Structural lints + plan-shape metrics                  | Synthetic graphs: chain, star, diamond, singleton                                             |
| D3   | `apply` (graph → issues/project), idempotent           | Replay tests against a recorded GitHub API fixture                                            |
| D4   | Readiness resolver as a pure function                  | Unit tests over synthetic graphs incl. diamond, cycle                                         |
| D5   | Cloud dispatch + workflow                              | One real task end-to-end on a throwaway plan                                                  |
| D5.5 | Block version key, configurable fence/paths, `doctor`, `init` | `init` against a synthetic board, then `doctor` green on the result                    |
| D6   | Local daemon: claim, worktree, heartbeat, spend gate   | One real capability-gated task end-to-end                                                     |
| D7   | Timeout/retry/stuck + stale-heartbeat reclaim          | Fixture with a stalled dispatch and a dead daemon                                             |
| D8   | Lark alerting with transition-only dedupe              | Fixture asserting one alert, not one per cron pass, plus a non-zero `code` treated as failure |
| D9   | `verify: auto` merge + subset/fence/drift guardrails   | Negative tests: red CI, fenced path, scope drift                                              |
| D10  | Plan-authoring contract: schema doc + agent skill      | An agent given only the doc produces a graph `validate` accepts unaided                       |
| D11  | `doctor` completeness, then interactive gated `init`   | `doctor` red on each defect in turn; `init` refuses to advance past one                       |
| D12  | Org + GitHub App auth                                  | A probe first: an App installation token assigning Copilot on a live issue                    |

D1–D5.5 and D9 are shipped. D7 is shipped except its stale-heartbeat half, which has no daemon to
reclaim from and waits on D6. **D6 is deferred by choice, not blocked**, and is now sequenced last:
it is the only long-running component, the only place per-task `model`/`effort` could live, and the
sole consumer of the `dispatch:local` label nothing reads today. D8 is worth building now that D7
produces `Stuck` — the first state worth waking someone for. Order from here: D10, D11, D12, D8, D6.

### D1 decision record — schema + validator (shipped)

`src/dispatchkit/` splits along the same seam the rest of the repo uses: `model.py` holds pure
frozen value objects (`TaskId`, `Lane`, `Verify`, `Dependency`, `Task`, `TaskGraph`) with no I/O,
`parse.py` does strict TOML → graph, `validate.py` does semantic invariants, and `cli.py` is the
only module that touches argv or the filesystem. `make dispatch-validate GRAPH=<file>` (or
`uv run dispatchkit validate <file>`) is the binary-exit-code command: 0 valid,
1 invalid graph, 2 unreadable file — "couldn't read it" is kept distinct from "read it and it's
wrong" because a workflow wants different responses to the two.

Four choices worth recording:

- **The schema is closed.** Unknown keys are errors, not ignored. A typo'd `verifiy = "auto"`
  that silently parses as `human` would only surface as a merge that never happens.
- **Both halves report every issue at once.** The graph file is the human review gate, so
  fail-fast would turn one review into N. Parse-level problems (shape, type, enum, unknown key)
  raise `GraphError` carrying the whole list; semantic ones are returned as a list.
- **Cycle detection is suppressed when an edge is dangling or self-referential.** Those already
  explain the malformed shape, and a cycle report on top of them is noise. Reported cycles name
  the path (`a -> b -> a`), per "a cycle is a hard error naming the cycle".
- **The routing rule is checked, not just documented.** `lane = "cloud"` with any `requires` tag
  is `lane-capability-mismatch`; unknown tags are rejected against the capability list, so the
  lane review is checkable rather than a matter of opinion.

`empty-acceptance` is an error rather than a lint, since "no acceptance criterion, no plan" is
what makes `verify: auto` possible at all. Structural lints and plan-shape metrics stay out of
D1 on purpose: they are warnings that inform a human, which is a different contract from an
invariant that blocks a push, and they are D2.

### D2 decision record — lints + plan shape (shipped)

`metrics.py` computes the shape (`plan_shape`, `levels`, `max_antichain`), `lints.py` the
warnings; both are pure functions over a validated graph, and `validate` now prints the shape
line plus any lints. Lints stay warnings — `--strict` promotes them to exit 1 for a caller that
wants the harder gate, but the default keeps D1's invariants as the only thing that blocks.

- **`width` is the exact maximum antichain, not the widest layer.** Longest-path layering is only
  a lower bound on how wide a graph can run (`a→b→c`, `d→c`, `a→e` layers 2 wide but has the
  antichain `{b, d, e}`), and a metric whose whole job is to expose parallelism must not
  under-report it. Computed via Dilworth — max antichain = n − maximum bipartite matching on the
  transitive closure — with König's construction returning the witness set, so a report can name
  which tasks the claim rests on. Graphs are dozens of nodes, so the cubic cost is free.
- **`depth` counts tasks on the critical path**, using longest-path levels: a task is only
  reachable once its slowest prerequisite chain lands.
- **Review sessions are batched by level, not counted per task.** Human-verify tasks sitting at
  the same level are reviewable in one sitting, so the estimate counts distinct levels — which is
  what makes the documented `12 tasks, depth 4, width 5, 3 human-verify, est. 2 review sessions`
  arithmetic work.
- **`merge-candidate` is suppressed on a chain.** Every adjacent pair of a chain is a merge
  candidate, so emitting them alongside `chain-graph` would bury one real finding under N
  restatements of it. Same reason `mostly-serial` defers to `chain-graph`.
- **The economic-floor lint needs an estimate, so `estimate_minutes` became an optional schema
  key.** Where estimates come from is open question 3; absent one the lint says nothing rather
  than guessing, and the floor is derived (`floor_multiple × overhead_minutes`, default 3 × 10m)
  rather than hard-coded, so it can be recalibrated from dispatch history later.

### D3 decision record — `apply` (shipped)

`block.py` renders and parses the machine block, `github.py` holds the state snapshot, the
operation union and the `GitHubApi` port, `apply.py` is the pure planner plus a thin executor,
and `gh_cli.py` is the adapter. `dispatch apply <graph>` is a **dry run unless `--push`** is
given, and on a dry run no client is constructed at all, so it cannot reach the network even by
mistake. Proven by replay tests against a recorded fixture, with the network blocked.

- **Idempotency is proven by convergence, not asserted.** Planning is a pure function of
  `(graph, RepoState)`; the test applies the plan to an in-memory double, re-reads the state,
  plans again, and requires the second plan to be empty. That is a stronger claim than "the code
  looks idempotent", and it is what catches an unstable body renderer — hence the separate test
  that rendering is byte-stable, since `apply` diffs bodies to decide whether to update.
- **The block is parsed by a closed grammar, not a YAML loader.** Issue bodies are
  attacker-influencable, and the block is `key: value` with scalars and flow lists — a fixed key
  set plus a fixed value grammar is a smaller attack surface than a safe loader plus a schema
  check, and it needs no dependency. Output remains valid YAML. Two blocks in one body is a hard
  error rather than "use the first", because "the first" is exactly what an injected second block
  would rely on; the free-form `touches` values are escaped so a smuggled `-->` cannot terminate
  the block early.
- **`apply` never writes `Status` or `Attempts`.** They are derived scheduling state the
  scheduler recomputes every pass, so writing a readiness guess here would only create a value
  that goes stale between passes.
- **`apply` never deletes, and never touches a closed issue.** Closed means done; an issue whose
  task has left the graph is reported as `orphan-issue` for a human. Both follow from the
  least-privilege token and from the rule that only PRs mutate the tree.
- **Label ownership is explicit.** `apply` owns `dispatchkit`, `plan:*`, `lane:*` and `verify:*`, and
  an update replaces exactly those while preserving `spend:approved`, `dispatch:stuck` and
  anything else a human or the scheduler applied. Stale owned labels ride along in the operation
  as `remove_labels`, so the adapter needs no extra read.
- **Nothing reaches a shell.** Every `gh` call is an argv list, and issue bodies go over stdin
  rather than argv — they contain newlines and backticks and would otherwise sit in the process
  table.

One honest gap: `tests/fixtures/dispatch/search_issues.json` is GraphQL-shaped but was generated
from the renderer, not recorded from the real API, since no repository or Project board exists
yet. It must be re-recorded from a live `gh api graphql` response when D5 stands up the scratch
repo; until then `GhCli`'s subprocess paths — particularly the Project field/option id lookups —
are unverified, and only its pure halves (payload parsing, argv construction) are under test.

### D4 decision record — readiness resolver (shipped)

`resolve.py` is the scheduler's brain with no I/O in it: `build_items` turns a repo snapshot into
`TaskItem`s, `resolve` derives every task's `Status`, `reconcile_ops` says what the board should
be told, and `admit` chooses what to start. `config.py` reads `dispatchkit.toml`. `dispatch resolve
--state <payload.json> --plan <name>` runs the whole pass against a recorded snapshot and is
read-only in every mode — dispatching is D5's job, so nothing here can start work by accident.
Proven by unit tests over synthetic items (chain, star, diamond, cycle) plus CLI tests over a
recorded payload, with the network blocked.

- **Status is derived, never stored.** It is recomputed from the issues on every pass, so the
  Project board is a projection: delete it and it rebuilds, hand-edit it and the next pass
  overwrites it. There is no state machine to get wedged because there is no state — which is
  what makes the whole scheduler a pure function of GitHub's own data.
- **Assignment is the lock.** A task is ready only while it is *unassigned*, so dispatching it
  removes it from the ready set. Two schedulers racing on one repo therefore converge instead of
  double-dispatching, with no lease, lockfile or mutex: GitHub's own write is the mutual
  exclusion. The local lane has no assignee to use, so it claims with a `dispatch:local` label,
  which the resolver treats identically.
- **Readiness is one step, not a recursive walk.** "Are my direct dependencies closed?" — nothing
  more. A hand-edited cycle (which `validate` would have rejected) is then harmless: every task
  in it stays `Blocked` and the pass still terminates. A dependency with no issue at all — never
  applied, or deleted — is not closed, so its dependents stay blocked. Failing safe beats
  guessing, and both cases are pinned by tests.
- **Admission is separate from status.** A task waiting on spend approval, or sitting at its
  retry budget, is still `Ready`: the work is unblocked, we are simply choosing not to start it.
  Folding those into `Status` would invent board values the plan does not define and would lose
  the distinction between *cannot run* and *will not run yet*. Deferrals are reported with a
  reason (`stuck`, `awaiting-spend-approval`, `lane-cap`, `file-scope-conflict`) so an idle board
  always explains itself.
- **In-flight work counts against the cap.** Caps bound what an agent is *doing*, not what this
  pass adds, so already-dispatched and in-review tasks occupy slots. Otherwise every pass would
  admit a fresh cap's worth and the queue would grow without bound.
- **File-scope exclusion compares literal prefixes, deliberately crudely.** Two ready tasks whose
  `touches` prefixes nest are serialised; disjoint trees run together; an empty `touches` means
  *unknown scope* and excludes nothing, because reading it as "conflicts with everything" would
  serialise the entire board. The heuristic errs toward over-serialising: an unnecessary wait
  costs minutes, a bad merge costs a human's afternoon.
- **Plan order is ascending issue number.** That is the order `apply` created them, which is the
  order they appear in the graph file — so the author's ordering is the tie-break and the choice
  is reproducible across passes.
- **`dispatchkit.toml`'s schema is closed, like the graph's.** Caps are the one dial that changes how
  much parallel work exists; a typo'd key silently reverting to a default is exactly the failure
  worth an error. An unconfigured lane admits nothing rather than defaulting to unlimited, and
  `cloud = 0` is the documented way to pause a lane without editing the graph.

The recorded payload gained `assignees` and `timelineItems` so the resolver's inputs are parsed
from realistic shapes, and `parse_state` tolerates their absence — an older recording degrades to
"nothing in flight" instead of failing mid-pass. The fixture is still renderer-generated, so D3's
gap stands: it must be re-recorded live at D5.

### D5 decision record — cloud dispatch + workflow (shipped, pending one live run)

`tick.py` is the pass: `plan_tick` is pure (snapshot in, operations out), `execute_tick` is the
only mutating half, `gh_cli` gained the agent-assignment path, and `.github/workflows/dispatch.yml`
runs it. `dispatch tick --plan <name> --state <payload.json>` is an offline dry run; `--push`
requires `--repo` and `--project` and is the only form that constructs a client.

- **Assignment is the lock, and the mutation is `replaceActorsForAssignable`.** *Replace*, not
  *add*: a pass that loses a race re-sends the same single actor, which is a no-op, rather than
  accumulating assignees. Combined with "ready ⇒ unassigned", that is the whole concurrency
  story — no lease, no lockfile. Proven by a test that runs two passes off the same snapshot and
  asserts exactly one assignment.
- **Operation order is load-bearing: dispatch first, record second.** Dying between the two
  leaves an assigned issue whose board entry is one pass stale, which the next pass fixes. The
  reverse — a board claiming `Dispatched` with nobody assigned — would let the next pass dispatch
  the same task again. The cheap failure is the one we choose.
- **A task dispatched this pass is recorded as `Dispatched`, not `Ready`.** Writing the resolved
  status would leave the board contradicting the issue for up to half an hour. The next pass
  derives the same value from the assignee, so the projection converges without a second write —
  which is what keeps the idempotency test honest.
- **A pass never creates, edits, or closes an issue.** `DispatchOperation` is deliberately
  narrower than `apply`'s `Operation` union: assign, label, set `Status`. `apply` owns the
  graph's shape and only a merged PR closes work, so the type system carries the rule rather than
  a comment.
- **The local lane is dispatched by label and nothing else.** The scheduler cannot reach the
  workstation, so `dispatch:local` *is* the handover; the daemon picks it up on its own schedule
  (D6). That keeps both lanes on one admission path instead of two schedulers.
- **The node id rides along in the snapshot.** The state query now selects `id`, so assignment
  costs no extra round trip. A snapshot without one — only reachable from a stale recording —
  produces a `missing-node-id` notice and no dispatch, rather than a lookup branch that nothing
  offline could exercise.
- **The workflow installs nothing.** `src/dispatchkit` is pure standard library, so the job that
  holds a token which can assign work runs no freshly resolved third-party tree. A test walks the
  package's imports and fails if that ever stops being true.
- **No `${{ }}` inside a `run:` script.** Expressions are substituted before the shell parses the
  line, so a value carrying a quote becomes a command. Everything arrives through `env:`, and a
  test asserts it. The workflow's permissions (`issues: write`, `repository-projects: write`,
  `contents: read` — never `contents: write`), its cron offset and its `cancel-in-progress: false`
  are asserted the same way, because all three are design decisions rather than formatting.
- **The token is `secrets.DISPATCHKIT_TOKEN`, not `GITHUB_TOKEN`.** The default token cannot assign
  the coding agent and cannot write a user- or org-level Project. `GhCli` fails loudly if the
  agent is not among the repository's assignable actors, naming the local lane as the alternative,
  rather than silently leaving work unassigned.

Two fixture bugs fell out of writing this, both worth naming. The synthetic issues shared one
Project item id, so a status write for one task landed on another — invisible until the
idempotency test refused to converge. And the first draft of the reconciliation test asserted
`Ready` for a task the same pass had just dispatched, contradicting the rule above; the test was
wrong, not the code.

**The gap:** the "one real task end-to-end on a throwaway plan" half of D5 has not been run —
it needs a repository, a Project board and an agent seat that do not exist yet. Everything
mechanical is proven offline, but `GhCli.assign_agent`, the Project field lookups, and the
workflow's own trigger wiring are still unexecuted code. `search_issues.json` also remains
renderer-generated. Standing the board up is the first task of the live run, and re-recording that
fixture from a real `gh api graphql` response is the second.

D1–D4 are pure and testable offline; only D5 onward needs a real repository. The planner skill
itself is evaluated separately and for free: run it against a requirements document that already
has a human-authored milestone breakdown, and diff its graph against that answer key. D9 lands
last on purpose: auto-merge is the only step that can mutate `main` unattended, so it ships only
after the dispatch loop has been observed working with a human on every merge, and after D8 can
tell us when it misbehaves. Enabling the merge queue and required-up-to-date branch protection is
a prerequisite for D9, not part of it.

### Extraction: dispatchkit becomes its own repository (post-D5)

D1–D5 were built inside `art_strategy`, the podcast/video project whose backlog motivated them.
They were extracted into this repository before D6.

- **Extract before D6, not after.** D6 adds a workstation daemon — a second install target with
  its own lifecycle — and the local lane's ergonomics are an adopter concern, not a host-project
  one. More importantly the machine block is a *wire format* written into other people's issues:
  every decision about it gets more expensive once issues exist. Nothing was live, so this was the
  cheapest hour it would ever be.
- **A tool that manages any repository cannot live inside one repository.** Nothing in D1–D5 knew
  about podcasts, but the packaging did: the CLI was `python -m tools.dispatch`, the config was a
  file in someone else's repo root, and the test tiering inherited the host's conventions.
  Adoption pressure was going to force the split regardless; doing it under pressure means doing
  it with issues already in flight.
- **Renamed in product and distribution.** `taskflow` is taken on PyPI (OpenStack's, 6.4.0).
  `dispatchkit` was free and matches the house style. The rename covered the wire format too —
  label, block marker and query filter — precisely because that is only free while unused.
- **Distribution is a reusable workflow first, a package second.** The unit of distribution should
  match the unit of execution. Most adopters need one workflow file and a config; only the D6
  daemon needs an installable package. The zero-dependency rule is what makes the workflow form
  viable at all.
- **Caps stay per-repo.** Per-owner caps would model spend more accurately but would force the
  state query from `repository.issues` to `ProjectV2.items`, coupling readiness to board hygiene
  rather than to issue truth. An adopter with several repos gets several independent caps; that is
  a documented limit, not a bug.

The extraction left three conventions that are now presumptuous and should become configuration
before the first outside adopter: the root `dispatchkit.toml` location, the
`docs/plans/<plan>.tasks.toml` graph path, and the hardcoded blast-radius fence paths — which,
having been written against the old config filename, no longer cover the config file itself.

### D5.5 decision record — pre-live hardening and the adoption on-ramp (shipped)

D5's remaining half is a live run against a repository, a board and an agent seat that do not
exist yet. Everything that makes that run cheap, safe or repeatable was pulled forward into one
step rather than discovered during it: the wire format got its version key, the three
presumptuous conventions became configuration, and the two commands that stand a repository up —
`doctor` and `init` — got built.

- **The block is versioned, and `v: 1` is the first key.** It is a wire format written into other
  people's issues, so the only free moment to add it is before any issue exists. A block claiming
  a version this build does not know is refused *before its other keys are read*, and reported
  alone: a newer writer may have changed what any other key means, so a partial read is a misread,
  not a degradation. A missing `v` is an ordinary missing key, because nothing in the wild
  predates it.
- **The fence is derived from the config, not written next to it.** The old list named
  `dispatch.toml`, a filename the config had not had since the rename, so the fence did not cover
  its own config file — the one file whose unattended edit would let the pipeline rewrite its own
  routing. It is now built from the settings it protects: both documented config locations, the
  configured plans directory, and `.github/workflows/**`. Moving `paths.plans` moves the fence
  with it, which is the property that makes the coupling worth having.
- **`fnmatch`, not a shell glob.** `*` crosses `/`, so every pattern matches more paths than an
  adopter probably intends. That errs toward fencing, and the only consequence of an
  over-broad fence is a human looking at a PR.
- **`.github/dispatchkit.toml` is the home; the repository root still works.** The config claims a
  filename in somebody else's tree, and `.github/` is the directory already conceded to tooling. A
  root config is still found, and is announced when it is used, because silently reading a
  different file than the one someone edited is worse than a line of output.
- **`set_project_field` fails with the option list in the message.** The silent version fell back
  to `--text` for any value it could not find an option id for, which `gh` rejects with a message
  about neither the field nor the value. It now raises `ProjectFieldError` — its own type because
  it is a *configuration* failure, where the fix is `dispatchkit init`, not a retry.
- **`doctor` is a pure function of a snapshot, so it is also the live tier's assertion set.** The
  whole check set runs offline against synthetic diagnostics; pointed at a real repository it is
  the same function over a real one. Two checks earn their asymmetry: an undeterminable token
  scope passes (a workflow token has no scope line, and failing every CI run over that is a false
  alarm), and a missing config passes (every setting has a default, so no config is a legitimate
  choice — the check exists to say which file *would* be read).
- **`init` never mutates a field it did not create, unless the board is empty.** GitHub's built-in
  `Status` ships with `Todo`/`In Progress`/`Done`: right name, wrong options, and no CLI path to
  add options to an existing single select. Fixing it means deleting the field, which deletes its
  values — free on an empty board, destructive on a populated one. So the item count decides, and
  a populated board gets a notice naming the `field-delete` command instead of an operation
  nobody asked for. `gh project view`'s item count is read *fail-safe*: an absent count reads as
  populated, so "unknown" can never license a delete.
- **`init` is split at the token boundary, not at the dry-run boundary.** `--local` writes the
  config, the workflow and the plans directory and touches no board, which is precisely the half
  that works before `gh auth refresh -s project` has been run — the first thing a new adopter
  hits. Ordering inside the board half is load-bearing once: a recreated field is deleted before
  it is created, because two fields cannot share a name.
- **The templates `init` writes are this repository's own files, pinned by a test.** The workflow
  it hands an adopter is byte-identical to the one `tests/test_workflow.py` asserts the
  permissions, cron offset and expression-injection safety of. Any other arrangement means the
  file under test and the file shipped are two files.
- **`doctor` and `init` are tested as one contract.** A test runs `init` against an in-memory
  board and a temporary tree and then asserts `doctor` passes on the result. `init` creating
  something `doctor` still complains about — or `doctor` demanding something `init` never
  creates — is the failure that makes an on-ramp worse than no on-ramp.
- **A separate `BoardApi` port, not more methods on `GitHubApi`.** Standing a board up and running
  a scheduler pass are different jobs with different blast radii; the pass should not be handed a
  client that can delete a field. `board.py` carries the contract and its port, above `resolve`
  because the required `Status` options are generated from the `Status` enum — a new status
  cannot be added without the board growing an option for it.
- **Only what the API reports is checked.** `gh project field-list` cannot distinguish a text
  field from a number field, so the field contract checks presence and, for single selects,
  option coverage — a subset test, since somebody else's extra option on our field costs nothing.
  Claiming to verify a field's kind would be a check that cannot fail.
- **The root join has exactly one owner.** Two bugs found in review, both from the same shape.
  `LocalFacts` is built from `--root`, so its paths are already absolute-or-rooted; `execute_tree`
  joined `--root` on again, and a relative root wrote everything to `<root>/<root>/` while the
  summary printed `<root>/` — reporting one thing and doing another, and never converging. Only
  the absolute `tmp_path` roots in the tests hid it. The fix is that the plan's paths *are* the
  paths written; `execute_tree` takes no root at all.
- **A `gh` failure is `doctor`'s answer, not its crash.** `token_scopes` was written to survive a
  logged-out `gh`, but `agent_available` and `fetch_board` go through `_run`, which raises. So
  precisely the first-adopter cases the command exists to name — no `project` scope, wrong project
  number, no board yet — produced a traceback. They now come back as a failing `board` check with
  its remedy, alongside the local checks, and exit 2 rather than 1: the board was not read, so the
  verdict is "could not be carried out", not "unhealthy".

**The gap this leaves:** `doctor` still cannot see branch protection or the merge queue, which are
D9's prerequisites; it will grow those checks when D9 needs them.

### D5.5 live correction — the built-in `Status` field cannot be deleted

The first `init --push` against a real board failed on its first operation, and the failure was in
the design, not the code:

```
GraphQL: Only custom fields can be deleted. (deleteProjectV2Field)
```

D5.5 had reasoned that a mis-optioned single select can only be fixed by delete-and-recreate, and
made it safe by gating on an empty board. Both halves were right except for the one field it
actually had to fix. Every Project arrives with a built-in `Status` holding `Todo`/`In Progress`/
`Done`, and GitHub refuses to delete built-in fields at all — empty board or not. So the operation
was unreachable in exactly the case that motivated it, and no offline test could have caught it:
the in-memory double faithfully implemented an API that does not exist.

`updateProjectV2Field` does work on built-in fields, and is the better primitive anyway:

- **The field keeps its id.** Delete-and-recreate would have silently orphaned every saved view,
  grouping and workflow the board had built on `Status` — a cost the old plan never accounted for
  because nothing offline could observe it.
- **The safety argument is unchanged.** Replacing the options still drops any value held under an
  option that goes away, so it stays gated on `board.items == 0`; on a populated board the notice
  now names the project's settings rather than a `field-delete` that would fail anyway.
- **The variables travel in a request body**, not in argv: `-F` cannot carry a list of objects,
  and the option list is one. `gh api graphql --input -` with a `json.dumps`ed body keeps the
  argv rule intact and keeps the field id out of the mutation text.

`delete_field` stays on the port. It is still the right operation for a custom field, and removing
it would trade a real capability for a tidier diff.

**Verified live 2026-09-08** against `bioshrek/dispatchkit-sandbox` and project 2: `doctor` named
the four missing fields and the five missing labels, `init --push` created them and set `Status`'s
options in place, `doctor` returned all `ok`, and a second `init --push` planned nothing. That
last one is the convergence standard met against real GitHub rather than a double.

**The gap this leaves:** `apply` and `tick` still have not run live, so the issue-writing and
dispatch paths — `create_issue`, `item_add`, `item_edit`, `assign_agent` — remain argv-checked and
unexecuted, and `tests/fixtures/search_issues.json` is still renderer-generated rather than
recorded.

### D5 live run record — the first real `apply` (2026-09-08)

Against `bioshrek/dispatchkit-sandbox` and project 2, with the `wordfreq` plan's five tasks.

- **`apply --push` created 5 issues, 5 project items and 15 field values**, then converged: a
  second run planned nothing. In between, a corrected graph produced `0 created, 5 updated, 0
  project items, 0 fields set` — the update path touching only what changed, which is the property
  that makes re-applying a reviewed graph safe rather than merely idempotent-looking.
- **The renderer-generated fixture had the right shape all along.** Comparing every path in the
  real GraphQL response against `tests/fixtures/search_issues.json` found *zero* divergence in
  either direction. That was the largest standing unknown in the project, since the fixture had
  been written from the same code that reads it and so proved only internal consistency.
- **The real response is now recorded** as `tests/fixtures/live_state.json`, with
  `tests/test_live_payload.py` asserting against it in the `replay` tier. It is kept *alongside*
  the generated fixture rather than replacing it: this snapshot is pre-dispatch, so it has no
  assignees and no PRs, and swapping it in would have quietly deleted the coverage of the
  dispatched and in-review states.
- **The machine block survived GitHub.** The block is written into a body that GitHub stores,
  renders and re-serves, which is exactly where an HTML comment or its whitespace could be eaten.
  Parsed back out of the recorded bytes it is intact, `v: 1` included.
- **`apply` writes `Task ID`, `Lane` and `Verify`, and deliberately not `Status`.** Visible in the
  recorded payload, and worth stating: `Status` is a derived view that a scheduler pass owns.

The first `tick` dry run, against that recorded state, is the design's own argument played back:

```
top-n: Dispatched      stopwords: Dispatched      encoding-fallback: Ready
json-output: Blocked   document-flags: Blocked
dispatch: top-n stopwords
  defer encoding-fallback: file-scope-conflict (overlaps top-n)
```

Three tasks were ready and the cloud cap was 3, yet only two dispatched — `encoding-fallback` and
`top-n` both touch `cli.py`, so `touches` deferred it. That is the advisory-scope rule doing on
real data exactly what it was designed to do, and it is the first evidence that the readiness
resolver, the dependency gate and the scope heuristic compose.

**The gap this leaves:** `tick --push` has still not run, so `assign_agent` remains the last
unexecuted mutation — and it is the one that spends a Copilot quota and starts autonomous work,
which makes it the right place to stop and ask.

### D5 complete — the first real dispatch (2026-09-08)

`tick --push` dispatched `top-n` and `stopwords`, deferred `encoding-fallback`, and wrote 5 board
values. The coding agent opened two draft PRs against the sandbox within the minute. The loop the
whole project exists to close — graph → issues → board → readiness → dispatch → agent → PR — has
now run once, unattended, on real infrastructure.

The second pass is the one that matters:

```
top-n: In Review     stopwords: In Review     encoding-fallback: Ready
dispatch: (nothing)
```

- **Assignment is the lock, confirmed against the real API.** Nothing was dispatched a second
  time, because both tasks had left the ready set the moment they were assigned. This is the
  property that lets concurrent passes converge without a lease or a lockfile, and it had never
  been tested anywhere but in memory.
- **`Status` is recomputed, never stored.** Between the two passes nothing wrote `In Review`; the
  status changed because the PRs appeared and the next pass re-derived it from the issues. The
  board is a view, and it moved on its own.
- **The deferral survived the state change.** `encoding-fallback` is still deferred, correctly:
  `top-n` is open, so the file-scope overlap still stands.

**One observation worth carrying into D7.** `replaceActorsForAssignable` is sent a single actor —
the agent — but the issue comes back assigned to *both* the agent and the human whose token made
the call. That is GitHub attributing the session, not a bug here, but reclaim logic that assumes
"assigned to the agent alone" would be wrong, and the retry/reclaim work should read assignees as
a set that contains the agent rather than equals it.

**The gap this leaves:** the pipeline has never been watched through a *completed* task. Nothing
has merged, so `verify`, the merge policy, retry and reclaim (D7–D9) remain unexercised, and
`tick`'s handling of a closed dependency releasing its dependents has been seen only in tests.

### D5.6 decision record — `verify: auto` had never been true (2026-09-08)

Watching the first dispatch settle turned up four faults. Every one of them was invisible to the
offline suite and none was a coding error: each was a place where the design had assumed
something about GitHub that is not so.

**`Auto-merging` was a claim nobody had checked.** `_status_of` returned `Auto-merging` for any
`verify: auto` task with an open PR, without ever looking at a check. GitHub classes the coding
agent as an untrusted contributor and parks its workflow runs pending human approval — so the
board would have sat on `Auto-merging` indefinitely for a pipeline that had not started and never
would. That is the board asserting something false, which is worse than the board being silent.

`Auto-merging` is now only claimed while CI can actually reach a verdict. A held or failing run
resolves to `In Review`, which is true in both cases because a human is the next mover either
way. This deliberately avoids inventing a board value: a new `Status` option would be a migration
for every existing board, and `In Review` already means what needs to be meant. The *reason* is
carried out of band, as a `ci-approval-required` notice — absorbing it into the status alone
would invite someone to go and review a pull request that cannot merge.

**`statusCheckRollup` cannot see this failure.** The obvious place to read CI's verdict returns
`null` for a held run. A run awaiting approval produces a check *suite* whose conclusion is
`ACTION_REQUIRED` and which contains **zero check runs** — and the rollup is assembled from check
runs. Through the rollup, a stalled pull request is indistinguishable from one in a repository
with no CI at all. The state query therefore reads `checkSuites` directly.

**`Checks.NONE` is not a stall.** Absence is ambiguous: with no stored state there is no way to
tell "the runs have not been created yet" from "this repository has no CI", and the first is the
normal condition in the seconds after a PR opens. Only GitHub saying `ACTION_REQUIRED` is treated
as a stall. An unknown suite conclusion counts as a failure rather than a success, because
GitHub adds enum members and guessing green would auto-merge on a verdict we have never seen.

**The shipped workflow could not run in any repository but this one.** `init` wrote a workflow
setting `PYTHONPATH: src`, with a comment explaining that there was nothing to install. True
here; false in every repository `init` exists to serve. The sandbox scheduler had been dying on
`No module named dispatchkit` on a cron since the moment it was installed, and three separate
things conspired to hide it: `doctor` only asked whether the workflow file existed, the failure
was on a schedule with nobody reading the log, and the one test that covered the template pinned
it byte-for-byte to *this repository's* workflow — asserting a sameness that cannot hold.

The template now fetches its own pinned source into `.dispatchkit`. Fetching is not installing:
no resolver, no build, no third-party code in a job holding a token that can assign work, so the
zero-dependency rule survives intact. The byte-identity pin is replaced by a shared set of
properties asserted against *both* files, plus the properties that are true of only one. That is
a weaker-looking guarantee and a stronger real one: the old pin could only have been kept by
breaking the template.

**A field change on an already-boarded issue was unexecutable.** Latent since D3: `apply` learned
board item ids only from the `AddProjectItem` operations it performed in the same run, so a
`SetProjectField` for an item added by an *earlier* run had no id to write to. Every previous run
either created the issue (id in hand) or changed only the body (no field operation), so the path
was first taken when a task's `verify` was edited in place — and died with `KeyError`. The plan
now carries the ids the board already held.

**The obvious remedy does not work.** `POST /actions/runs/{id}/approve` answers 403 — *"not from
a fork pull request or queued by the Actions bot"*. The Copilot gate is a different class from
the fork gate and that endpoint does not clear it. `gh run rerun <id>` does: it re-queues the run
under the maintainer's own identity. Verified live, and the notice says so, including that
clearing the gate either way is a decision to run agent-authored code rather than a formality.

**What this says about `verify: auto` generally.** It cannot be relied on while the gate stands,
because a green pipeline requires a human click *per run*. There are three ways out and they are
not equivalent: a repository setting that skips approval for coding-agent workflows (a blanket
trust decision, and not exposed over REST, so `doctor` cannot check it and `init` cannot set it);
the agent verifying itself (the agent grading its own homework, which is what `auto` exists to
avoid); or dispatchkit running the verification itself. The last is the only one that both keeps
verification independent and works unattended: a workflow on a *trusted* trigger is not gated, so
a job that takes a PR number, checks out the merge ref and runs the task's declared `verify`
command would sidestep approval entirely — dispatchkit is asking, not the agent. The condition
that makes it safe is the one already in force elsewhere: that job runs untrusted code, so it
holds no token and no secrets and reports by exit code to the pass that does. This is why
`pull_request_target` is the wrong answer and is ruled out. Left for D9, where its real cost
belongs in the open: it would make `verify` a command dispatchkit executes rather than a claim
about the repository's own pipeline, and two CIs can disagree.

**The first way out was taken on the sandbox, and it works.** The setting is
Settings → Copilot → Cloud agent → "Actions workflow approval" → *Require approval for workflow
runs*; it is per-repository and off by default. Notably it is *not* the fork-PR control under
Actions → General, which is a genuinely different mechanism — `fork-pr-contributor-approval`
remained `first_time_contributors` throughout and changing it would have done nothing. That
distinction is the same one the 403 was reporting.

Verified by making Copilot push rather than by pushing as a maintainer, since a maintainer push
runs regardless and would have proved nothing: a review comment on PR #7 asking for a missing CLI
test produced a commit whose CI went straight to `success`, no `action_required`, no human move.
Both open PRs then read `COMPLETED/SUCCESS`, the pass reported `top-n: Auto-merging` and
`stopwords: In Review` with zero board writes, and `ci-approval-required` correctly went silent.

This does not retire the D9 argument; it narrows it. The setting is a blanket trust decision
whose cost GitHub states plainly — unreviewed agent code may gain write access or reach Actions
secrets. It is acceptable here because the scheduler runs on `schedule`, not `pull_request`, so
`DISPATCHKIT_TOKEN` never reaches a Copilot PR, and that separation is now load-bearing rather
than incidental. The case it does not cover is a Copilot PR editing `.github/workflows/`, which
then runs unreviewed. So `verify: auto` is now usable unattended for adopters willing to make
that trade, per repository and by hand, while D9 remains the answer for those who are not.


### D5.7 — a draft pull request merges nothing

Found by asking why the two sandbox PRs were still drafts. The answer is that this is correct
behaviour and nothing was stuck: the timeline on #6 reads `copilot_work_started`, `committed`,
`renamed`, `copilot_work_finished`, `review_requested`, with no `ready_for_review` after it.
Copilot finishes, requests review, and deliberately leaves the pull request a draft for a human
to mark ready. GitHub documents this.

The bug was ours, and it is D5.6's bug reached by a second road. `Auto-merging` had just been
taught to check that CI could reach a verdict; it still did not check that a merge was *possible*.
A draft cannot be merged at all, so green CI on a draft merges nothing and the board would sit on
`Auto-merging` forever — the same false claim D5.6 existed to remove, with a different cause.
Confirmed rather than assumed:

```
$ gh pr merge 7 --auto --squash
GraphQL: Pull Request is still a draft (mergePullRequest)
```

**`mergeStateStatus` cannot be used to detect this,** which is the trap worth recording. GitHub
documents a `DRAFT` value for it, but both live PRs reported `mergeable: MERGEABLE` and
`mergeStateStatus: CLEAN` while `isDraft` was true. This is the same shape of failure as
`statusCheckRollup` returning `null` for a held run: the aggregated field looks like the one to
read, and it is quietly wrong for the case that matters. Both times the fix was to stop reading
the summary and ask the specific question. `isDraft` is now a field on `PullRequest`, parsed
verbatim, never inferred. A missing `isDraft` reads as *not* draft, because the pessimistic
reading would strand `verify: auto` at `In Review` forever over a payload shape we merely failed
to request.

**Clearing the gate is `verify: auto`'s whole meaning.** For `verify: human`, draft is exactly
right and no notice is emitted: marking it ready *is* the reviewer's act, and a notice on every
such task would be noise. For `verify: auto` there is no reviewer by definition, so leaving the
draft would define a task that can never close. `auto` declares that the pipeline decides, and
the pipeline has decided; the draft is therefore a gate dispatchkit has already been told it may
clear. `ready_ops` emits `MarkReady`, and `gh pr ready` performs it.

Guarded on `Checks.PASSING`, not on "not stalled". `Checks.NONE` is an absence of evidence, and
clearing a merge gate on the strength of no runs at all would be a worse version of the fault
being fixed here. This narrows behaviour for one case — a draft with no CI and `verify: auto`
used to reach `Auto-merging` and now stays `In Review` — which is the honest answer, since
nothing verified it.

`MarkReady` is the first operation a pass performs against a pull request rather than an issue.
The rule it does not break is the one that matters: it changes no content, creates nothing and
closes nothing, so `apply` still owns the graph's shape and only a merged PR still closes work.
It is counted as `readied` rather than `dispatched`, because no work was handed to an agent and
the dispatch count is the number a human skims in the workflow log.

The board is told the status that was true when the state was read, so a pass that clears a draft
still reports `In Review` and the next pass reports `Auto-merging`. That is the same convergence
the resolver relies on everywhere else, and it is proven twice: against the in-memory double, and
live —

```
pass 1: top-n: In Review     0 dispatched, 1 board write(s), 1 PR(s) marked ready
pass 2: top-n: Auto-merging  0 dispatched, 1 board write(s)
pass 3: top-n: Auto-merging  0 dispatched, 0 board write(s)
```

with PR #6 (`auto`) ending `draft=false, mergeable=MERGEABLE` and PR #7 (`human`) untouched at
`draft=true`. `Auto-merging` is now, for the first time, a claim that is true of both the pipeline
and the pull request.


### D5 live: the graph moved

The first task to go all the way through. PR #6 was reviewed against its own declared acceptance
— run locally rather than read off CI, on the principle that a green check is evidence about a
pipeline and not about the work — and against its task body: the limit stayed in `cli.py`,
`count_words` was left returning the unsliced list `json-output` will need, `--top 0` prints
nothing, a negative N exits 2. Squash-merged, and `Closes #1` closed `top-n` without help.

The pass that followed exercised three mechanisms that had only ever run against synthetic
graphs, all at once:

```
top-n: Done
encoding-fallback: Dispatched
json-output: Ready
dispatch: encoding-fallback
  defer json-output: file-scope-conflict (overlaps encoding-fallback)
pass complete: 1 dispatched, 3 board write(s)
```

Dependency unblocking: `json-output` went `Blocked → Ready` because its one dependency closed.
Deferral release: `encoding-fallback` had been held every pass for overlapping `top-n`, and the
moment that overlap merged it was dispatched. And the part worth pausing on — **the file-scope
fence re-formed around the new pair in the same pass.** `json-output` was immediately deferred
against `encoding-fallback`, because both touch `cli.py` and `tests/test_cli.py`. The exclusion is
not a property of any task; it is recomputed from whatever is in flight, so it moves as the front
moves. That is what having no stored state buys, demonstrated rather than argued.

Copilot opened PR #8 within the minute. The next pass dispatched nothing and wrote once:
assignment is still the lock.

**What this did not prove.** `Auto-merging` remains a claim nothing fulfils — there is no merge
code in the package, and the sandbox has `allow_auto_merge: false` and no branch protection, so
no required check exists for a merge to wait on. `top-n` reached `Done` because a human merged
it. That is the same unchecked-claim pattern D5.6 and D5.7 removed, sitting one level up, and D9
is where it gets settled.

One further observation, recorded because it is the next candidate for the same treatment:
`encoding-fallback` reported `In Review` while its pull request was still titled `[WIP]` and the
agent was still pushing to it. Nobody can review that. `draft` alone cannot distinguish it, since
Copilot also leaves a *finished* PR in draft; the signal that separates them is the
`copilot_work_finished` timeline event, which the state query does not currently request.


### D9 — the merge, and what the repository does not enforce

`Auto-merging` finally does something. `merge_ops` emits a `MergePr` for a `verify: auto` task
whose pull request is out of draft, green, mergeable and outside the blast-radius fence, and the
adapter performs a direct squash merge. Live, `stopwords` went from `Auto-merging` to `Done`
without a human: the pass reported `1 PR(s) merged`, the squash closed issue #2 through
`Closes #2`, the next pass read `Done`, and the one after that wrote nothing.

**The guardrail this deliverable was built on turned out to be false.** The design said branch
protection was the real enforcement and that a misconfigured scheduler fails closed. Probing a
throwaway pull request on the sandbox showed the opposite, in the worst direction:

```
$ gh pr merge 9 --auto --squash      # allow_auto_merge: false, no branch protection
$ gh pr view 9 --json state,mergedAt
{"mergedAt":"...","state":"MERGED"}
```

No output, exit 0, merged instantly, no check consulted. `--auto` does not mean "merge once the
requirements are met"; where there are no requirements it degrades to "merge now". The GraphQL
mutation it is named after refuses that exact case — `Auto merge is not allowed for this
repository` — so **the CLI flag is strictly less safe than the thing it wraps**. That is why the
adapter merges directly and why `merge_command` has a test asserting the *absence* of `--auto`.

A second flag is absent for a related reason. Branch protection refuses a red pull request even
for a repository admin, and the scheduler's token usually belongs to one — but `--admin`
overrides precisely that. Without omitting it, the second lock would not exist.

The two calls turn out to be complements rather than alternatives, which is worth stating because
it is not documented. `enablePullRequestAutoMerge` is refused on a pull request that is `CLEAN`
("Pull request is in clean status") and accepted on one that is `BLOCKED`; a direct merge is the
reverse. Auto-merge is for the window while checks are *pending*. Dispatchkit polls on a cron and
therefore sees settled states, so the direct merge is the one that fits — and it is the one branch
protection independently gates.

**So the enforcement story is inverted from the design's.** Every gate is checked in `merge_ops`,
which stands on its own; branch protection is a *second* lock where it exists, never the first.
`doctor` gained `merge-gate`, which reports an unprotected branch not as broken but as
single-gated: dispatchkit's reading of CI is then the only thing between an agent and `main`.
`init` cannot fix this either — it cannot know which status check an adopter's `acceptance` runs —
so it joins `NEXT_STEPS`.

**Then the live run found the same old bug a fourth time.** `stopwords` was green, out of draft,
outside the fence — and `CONFLICTING`. The pass asked GitHub to merge it and took a twelve-frame
traceback, which also discarded every board write queued behind the merge. Two fixes: `merge_ops`
and `_status_of` now require `mergeable`, and a refused merge became a `Notice` rather than an
exception, because a conflict is an ordinary outcome and a pass must survive it.

That is D5.6 (CI unchecked), D5.7 (draft unchecked) and now mergeability unchecked — three
different fields, one habit. The generalisation is now explicit: *green is not a synonym for
anything else.* Each precondition for a merge is a separate question, and `mergeable` defaults to
`False` on `PullRequest` so that `UNKNOWN` — GitHub still computing — waits a pass rather than
being guessed at.

**What the conflict was actually evidence of.** `stopwords` declared
`touches = ["src/wordfreq/count.py", "tests/test_count.py"]`, and its pull request edited
`src/wordfreq/cli.py` and `tests/test_cli.py`. `top-n` edited `cli.py` too. The file-scope
exclusion never fired, because it reasons about *declared* scope and the agent had drifted outside
its own — so the two ran concurrently and collided, and a human had to resolve it by hand.

This is the `subset/fence/drift` row of the D9 plan, and only two of the three shipped. Drift is
now the one with live evidence behind it: the check is that a pull request's file list is a subset
of its task's `touches`, and dispatchkit already reads both. It is the next thing to build, and it
would have prevented this conflict rather than merely reporting it afterwards.


### D9.1 — the two guardrails that make `verify: auto` mean something

D9 shipped the merge with one of its three guardrails. The fence was there; `subset` and `drift`
were not, and both turned out to have live evidence waiting for them.

**Acceptance must be a subset of CI.** `merge_ops` merges on green check suites, and nothing
established that those suites run the task's own `acceptance`. On a repository whose CI does not
cover it, "green" means "something unrelated passed" — and dispatchkit would have merged on that.
The validator now refuses `verify = "auto"` unless every `&&`-clause of `acceptance` is covered by
a command CI runs.

Pointed at the live sandbox it failed on its first run, which is the point:

```
acceptance-not-in-ci: [document-flags] `verify = "auto"` merges on green CI, but not run by CI:
  `grep -q -- '--top' README.md`; `grep -q -- '--stopwords' README.md`; ...
```

The graph's own comment had said `auto` was safe "because the acceptance is a grep: there is
nothing here for a human to judge". That reasoning was right about *judgment* and wrong about *who
runs the grep* — nobody did. The remedy is the general lesson: **an acceptance CI cannot run is a
sign the acceptance is not a test.** `document-flags` now asserts the README's contents from
`tests/test_readme.py`, so the check lives in the pipeline that decides the merge.

Two details are load-bearing. Coverage is *prefix*-based, so a task may narrow what CI runs
(`uv run pytest -q` covers `uv run pytest -q -k stopword`) but never widen it; that is a
conservative approximation, and it is wrong in the safe direction, since its failure mode is
refusing a task CI does cover. And only workflows that trigger on `pull_request` count —
otherwise the hole reappears in miniature, with a cron-only workflow's `run:` steps satisfying a
rule about checks that never appear on the pull request. Both are scanned off the YAML text rather
than parsed, because `src/dispatchkit` is pure standard library and `pyyaml` is dev-only.

**Scope drift.** `merge_ops` now refuses a pull request whose files are not all inside its task's
declared `touches`. The evidence is the collision from the D9 live run: `stopwords` declared
`count.py` and `test_count.py` and its pull request edited `cli.py` and `test_cli.py`. The
concurrency exclusion reasons about *declared* scope, so it had nothing to go on, and `stopwords`
ran alongside `top-n` — which was editing `cli.py` — until they conflicted and a human untangled
them.

Checking the rest of the graph afterwards sharpened the point: `encoding-fallback` declared
exactly the two files its pull request touched, and `stopwords` had drifted into *those same
files*. So one agent's undeclared edit defeated two separate exclusions at once. Declared scope is
only as good as the declaration, and this is the check that makes the declaration binding.

An empty `touches` refuses to merge. It reads as "conflicts with nothing" for the concurrency
exclusion, which is the right permissive answer to a scheduling question; here it is an
unanswerable question about whether an agent stayed where it said it would, and the answer to
those is no.


### Decisions taken about work not yet built

Recorded here because each was argued to a conclusion, and re-arguing them later without the
reasoning would be waste.

**The local lane is an empty socket, and that is the whole answer to "can we choose the model?"**
`_dispatch_op` handles `Lane.LOCAL` by adding a `dispatch:local` label and nothing else — the
scheduler cannot reach a workstation, so the label *is* the dispatch, and the claim check treats
it exactly as assignment is treated for cloud. The symmetry is deliberate and preserves the
assignment-is-the-lock property. But no daemon consumes it, so `caps.local = 1` is not a
conservative default, it is moot.

This settles the model/thinking-effort question. For cloud, dispatch is "assign Copilot to this
issue" and there is no parameter to carry a model on. For local, *we* would write the runner, so
we own the invocation and per-task `model`/`effort` become ordinary graph fields. It is therefore
a local-lane feature, and an argument for building the daemon rather than a separate deliverable.

**Setup can be gated by `doctor`, except at one step, permanently.** Making `doctor` the single
source of truth and having `init` refuse to advance past a failing check is strictly better than
today's print-and-hope. But the Copilot workflow-approval setting is not exposed over REST, which
is why it already lives in `MANUAL_STEPS` apart from the steps with a copyable command. That step
can only ever be asserted by a human. Since it is also a genuine trust decision — turning it off
lets unreviewed agent code run your workflows — a forced pause there is arguably correct, and the
design should say so rather than imply the gate is complete.

**Org-first, not org-only.** The awkward token has one root cause: user-owned Projects reject
fine-grained tokens, which forces a classic PAT holding broad `repo` and `project` scope. An org
Project with a GitHub App removes that, giving short-lived installation tokens and per-repo scope
— a security improvement, not merely fewer steps. But dropping user repos is an amputation, and
the load-bearing unknown is whether an App installation token can assign Copilot. Probe before
committing; if it cannot, the migration buys nothing.

**No external workflow engine.** Temporal, or anything like it, would make scheduling across many
repos look simpler and would contradict the property the design rests on: there is no stored
state. Status is recomputed from the issues every pass, and assignment itself is the lock, which
is why concurrent passes converge with no lease. A workflow engine adds a second source of truth
about what is running, and the moment it disagrees with GitHub there is a reconciliation problem
this design does not currently have. Many repos are already served by addition rather than
coordination: each carries its own cron, independent and stateless. If the pain turns out to be
seeing every board at once, that is a read-side aggregation problem and does not require moving
the scheduler. Reconsider only if D7 surfaces state GitHub genuinely cannot express.


### D7 — the stall, and a counter nothing was writing

The retry budget was there from D4: `_gate` refused to admit a task whose
`attempts` had reached `retry.budget`. It read that number off the Project
board's `Attempts` field, **and nothing ever wrote it.** It was zero on every
task on every pass, so the budget could not fire, and a dispatch that produced
nothing sat assigned forever showing `Dispatched`. The gate was real code
guarding a number that was structurally always zero — the same fault as the
board asserting a state it never checked, one layer down.

**Attempts are derived from the issue's assignment history, not stored.**
Writing the counter to the board would have been the obvious fix and the wrong
one: the board is a derived view, so a number only it remembered would be the
single piece of scheduler state GitHub could not rebuild, and the "no stored
state" property would be gone for a `+= 1`. The timeline already records every
assignment, so `attempts` is `len(dispatches)` and the board's `Attempts` field
becomes a display of it.

**Two probes, two traps, both live.** Querying `ASSIGNED_EVENT` on the sandbox
showed that one dispatch produces *two* `AssignedEvent`s — the bot and the
human who triggered it. Counting events wholesale would have scored every
attempt twice against a budget of three, halving it. Then reading the parsed
state showed the agent appears under **two different logins depending on the
field**: `copilot-swe-agent` in the timeline, `Copilot` in the assignee list.
`AGENT_LOGINS` holds both, which is what makes the count right *and* keeps a
reclaim from unassigning a human who was watching the issue.

**Releasing the lock is the retry.** Assignment is the dispatch lock, so
`UnassignAgent` is the entire mechanism: the task rejoins the ready set on the
next pass with nothing else written down, and the attempt it just spent is
still counted because the timeline is permanent. The convergence test is
therefore unusual — the second plan is deliberately *not* empty — so what it
pins is that each cycle costs exactly one attempt and the budget terminates.

**The timeout asks whether anything was produced, not whether it finished.** A
task with an open pull request is never reclaimed however long review takes;
reclaiming there would throw away real work. Only a dispatch with no pull
request at all can stall.

**`Stuck` is a status, not just a label.** A stuck task is unassigned, so
without a status of its own it satisfies every readiness test while never being
dispatched again — the board advertising queued work that cannot move. The cost
is a board migration, and `init` refused to perform it: adding an option to a
populated single-select would delete the values of the 5 items using it. That
refusal is correct, and on the sandbox it also proved cheap to override — the
option was replaced by hand, all 5 values were lost, and the next pass rebuilt
every one of them from the issues alone. A destructive migration of the board
costs nothing precisely because the board never held anything.


## First real plan

Dispatchkit is the priority; video generation is its payload. Two unfinished systems built at once
is how each one's bugs get blamed on the other, so the ordering is explicit: the video generation
requirements (`docs/video_generation_requirements.md` in the `art_strategy` repo, dispatchkit's
first adopter) wait, and are used as the first genuine plan rather than being hand-run in
parallel.

It is a strong candidate because it already clears the gate that blocks most requirements —
**every phase ends in a command with a binary exit code** — along with a risk register, AC11/AC12,
and an explicit human-involvement table. The expensive human judgment is already spent.

But most of it is a poor _first_ trial: Remotion/Chromium, ffmpeg, a 34 GB content tree and a
gigabyte of gitignored reference MP4s put it squarely in the local lane, and much of it is
`verify: human` by nature (background art, wave sign-off, still calibration). Its **Phase 0** is
the exception and is close to ideal: pure Python, no Node, no network, no spend, operating on
artifacts already on disk, ending in a differential test. It also has real width — vendor
fonts/music, calibrate style tokens, derive the wave mask, freeze the `spec.json` schema, build
`VideoSpec` — and is textbook interface-first, with the schema landing before the fan-out.

Worth noting for routing: that plan's own step of reducing the differential oracle to a ~1 KB
numbers file is what makes downstream tasks cloud-eligible. Before it they need the gigabyte
MP4s and carry `local-data`; after it they don't.

| When             | Trial                                                             |
| ---------------- | ----------------------------------------------------------------- |
| Now, before code | Hand-run the planner on it; diff against human instinct           |
| After D1–D2      | `validate` a hand-written Phase 0 graph — shape only, no dispatch |
| After D5         | Dispatch Phase 0's cloud-eligible tasks for real                  |
| After D6         | Phase 1 and the W spike, exercising the local daemon              |
| After D8–D9      | Phases 2–3, where render cost and human sign-off matter           |

### The planner's golden fixture

That document keeps its original hand-written requirement verbatim as Part 1 alongside the
derived design as Part 2. Feeding the planner the finished phase table proves little — it would be
copying. Feeding it **Part 1** and seeing whether it arrives at anything resembling Part 2's phases
is a real decomposition test with a human-authored answer key in the same file. That pairing, not
the phase table, is the fixture worth pinning.

## Decisions

- **Lane is proposed, not decided, by the planner.** It emits `lane` with a rationale; the human
  review of `*.tasks.toml` ratifies it. No lane assignment ever reaches GitHub unreviewed.
- **Merge authority is per-task, not global.** `verify = "auto"` lets the scheduler merge on
  green CI; `verify = "human"` (the default) always stops for review. Blanket human review would
  make the reviewer the throughput ceiling, so the design pushes work toward machine-checkable
  acceptance instead.
- **One Project board for all plans.** One query for the scheduler, one definition of the custom
  fields, and concurrency caps that bound total in-flight work rather than per-plan work.
  `plan:*` labels provide the per-plan views. Revisit only if independent burndown per plan
  becomes a real need.
- **Disjoint file scope is advisory, not enforced.** `touches` biases scheduling away from
  collisions; the merge queue, not the scheduler, is what guarantees a clean `main`. Making scope
  a hard constraint would trade real parallelism for a guarantee it can't actually deliver.
- **Lanes are defined by runner capability, not by domain or cost.** `requires` tags describe
  what a task needs (time beyond the cloud session cap, a specific OS, a GPU, unrestricted
  network, local data, interactivity); the cloud lane takes anything with no tags. `spend` is a
  separate axis, gated by `spend:approved` in either lane.
- **Liveness is a dead man's switch, not a health check.** The daemon writes heartbeats to a
  pinned status issue; the cron scheduler judges staleness. Each side alerts on the other's
  silence, because neither can report its own death.
- **Milestones and tasks are separate layers.** A milestone is a verification checkpoint defined
  by an acceptance criterion going green; a task is a dispatch unit sized by routing,
  verification and parallelism. Merging the two concepts would force a choice between a legible
  proof narrative and a wide dispatch graph.
- **Task boundaries must be justified, never chosen.** A split earns its overhead only through
  parallelism, routing, verification, size or revertability — so a serial chain is never split.
  Granularity is then reported as lints and plan-shape metrics rather than argued about.
- **Dependency edges must name the artifact they wait on.** Ordering by narrative plausibility
  rather than data dependency is the most common decomposition error and silently serialises the
  graph; an edge that can't name what it needs is deleted.
- **Dispatchkit before video generation.** Video generation is the payload that proves dispatchkit, not
  a parallel effort. Building both at once makes every failure ambiguous between them.
- **Test tasks are real chores, and the acceptance list is the failure table.** No-op tasks pass
  every interesting scenario by construction; a completed task is the least informative outcome
  the system can produce.

## Open questions

1. Retry budget of 3, the 30-minute heartbeat window, and the 24-hour missing-scheduler window
   are guesses; all three want tuning against real behaviour once D6–D8 are running.
2. Should a self-hosted Actions runner advertising `os:macos` / `gpu` eventually replace the
   local daemon, collapsing two lanes into one dispatch mechanism?
3. Where do per-task effort estimates come from? The economic floor lint needs one, and neither
   planner guesses nor historical medians are obviously good enough.
