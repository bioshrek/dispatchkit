# Resume notes

Written for whoever picks this up next. `docs/design.md` holds the design and the D1–D5.5
decision records; this file holds only what is *not* in there: current state, the open decisions,
and the exact next actions.

## Where things stand

D1–D5.7, D7, D9, D9.1/D9.2, D14, D13, D13.1a–e, D6.0–D6.5, D10 and D11 are implemented and green
offline. 866 tests, `ruff`, `mypy --strict`, `lint-imports` all clean via `make check`.

**Only D15 remains** — the plan retrospective, which measures dispatch overhead and work from the
timeline rather than from a schema key. It is unblocked: the sandbox's `wordfreq` plan is
finished, so there is a real timeline to measure.

D11 found three things `doctor` stayed green on, and the first is the one to know about: an
explicit `fence.paths` **replaces** the derived default rather than adding to it, so a narrow list
leaves the config itself unfenced — and a `verify: auto` merge may then rewrite `runner.argv`,
which `watch --local` executes on your machine. `check_fence` now tests representative paths
against the patterns, and `init` will not exit 0 over a failing local check. See the D11 record in
`docs/design.md`.

| Step | What | State |
|------|------|-------|
| D1 | Task graph schema + validator | done |
| D2 | Structural lints + plan shape | done |
| D3 | `apply` (graph → issues + board), idempotent | **done and proven live**; converges |
| D4 | Readiness resolver + admission | **done and proven live**; assignment is the lock |
| D5 | Cloud dispatch + workflow | **done and proven live**; agents dispatched, PRs open |
| D5.5 | Block version key, configurable paths/fence, `doctor`, `init` | **done and proven live**; board bootstrap converges |
| D5.6 | CI-aware status, workflow template fix, `doctor` inputs | **done and proven live**; `verify: auto` now means something |
| D5.7 | Draft-aware status, `MarkReady` for `verify: auto` | **done and proven live**; `Auto-merging` implies a merge is possible |
| D7, D9, D9.1 | Retry budget, `verify: auto` merge, the two guardrails | **done and proven live** |
| D14 | Retire the Project board | **done and proven live**; `doctor` green with no `project` scope |
| D13 | `watch`: the scheduler moves local | **done and proven live**; a pass ran from a terminal on `gh`'s credential |
| D13.1a | The looping report prints only what moved | done; an unchanged pass prints one heartbeat line |
| D13.1b | `Cancelled`: closed as not planned satisfies nothing | done; fixes a live over-release bug, offline-proven only |
| D13.1c | `dispatch:hold`: not now, and not charged an attempt | done; stops dispatch and auto-merge, offline-proven only |
| D6.0 | A lane with no executor is refused, not marked | done; fixes a silent permanent stall on `lane: local` |
| D6.1 | Runner argv template, model allowlist, env floor, `caps.local` invariant | done; the human decisions D6 needed are made |
| D13.1d, D13.1e | Clean-tree gate for `verify: auto`; the graph watcher re-plans on save | done |
| D6.2–D6.5 | The `Workstation` port, the executor, recovery, `watch --local` | done |
| D9.2 | Scope drift advises; the repo fence keeps the merge authority | done |
| D10 | Plan-authoring contract: `docs/schema.md`, body lints, `docs/authoring.md` | done; an agent given only the two docs produced a `--strict`-clean plan |
| D11 | `doctor` closes over the fence and a missing `gh`; `init` gated on the local checks | done; the fence override discarded every self-protection |
| D15 | Plan retrospective: overhead and work from the timeline | designed, unbuilt |

This repo was extracted from `~/Documents/py_repos/art_strategy` (where it lived as
`tools/dispatch/`) on 2026-09-08. `art_strategy` is intended to become adopter #1.

## Immediate next actions

The live trial runs against a **disposable sandbox**, not this repo:
[`bioshrek/dispatchkit-sandbox`](https://github.com/bioshrek/dispatchkit-sandbox), public,
created 2026-09-08 and checked out at `../dispatchkit-sandbox`. `tick` assigns a coding agent and
D9 will auto-merge, and none of that argv had ever been executed — a sandbox absorbs a first run
that misbehaves. Its payload is `wordfreq`, a deliberately unfinished CLI with real tests and real
CI (green in 13s), so the backlog is genuine chores rather than the no-ops the design forbids.

**The board bootstrap is done and proven live — and then deleted.** D14 retired the Project
board entirely, so nothing below is reachable any more; it is kept because the live corrections it
produced are the evidence for that decision. Sandbox project was
[number 2](https://github.com/users/bioshrek/projects/2), and deleting it is the one privileged
act D14 still leaves to a human. In order:

- The sandbox exists, is pushed, and its CI passes (13s).
- `init --local` wrote the config, workflow and plans directory where its summary said.
- `doctor` named the four missing fields and five missing labels, exit 1.
- `init --push` **failed on its first operation** — GitHub refuses to delete the built-in `Status`
  field, so D5.5's delete-and-recreate was unreachable in the only case it existed for. Fixed by
  `updateProjectV2Field`; see the D5.5 live correction in `design.md`. The failure itself behaved:
  exit 2, nothing partially applied.
- `init --push` then created 5 fields and 5 labels; `doctor` returned **all `ok`**; a second
  `init --push` planned nothing. Convergence, against real GitHub rather than a double.

The shorthand the remaining steps use:

```sh
S="--root ../dispatchkit-sandbox --repo bioshrek/dispatchkit-sandbox"
```

The plan graph is written and applied: `wordfreq`, five tasks, issues #1–#5 on the board. The
fixture question is settled — the real response matched the generated one exactly, and the real
one is now recorded as `tests/fixtures/live_state.json`. See the D5 live run record in
`design.md`. The lints earned their keep while the graph was written, catching a serial pair split
for nothing and an estimate below the economic floor.

**Dispatch has run.** `tick --push` assigned `top-n` and `stopwords`; the coding agent opened
draft PRs #6 and #7 within the minute. A second pass dispatched nothing — assignment is the lock,
confirmed against the real API — and `Status` had moved itself to `In Review`, recomputed from the
new PRs rather than stored. See the D5 completion record in `design.md`.

**`verify: auto` has been made honest.** D5.6 found the resolver claiming `Auto-merging` for PRs
whose CI GitHub was holding pending approval — a pipeline that had not started and never would.
It also found the shipped workflow template unable to run in any repository but this one, which
is why the sandbox scheduler had been failing on its cron since installation. Both are fixed and
both were proven against the sandbox; see the D5.6 decision record in `design.md`.

Live sequence, for reference: `top-n` was moved to `verify: auto` (its acceptance is a test
selector and a linter, so `human` was over-conservative), and the pass then reported `In Review`
with a `ci-approval-required` notice rather than `Auto-merging`. After `gh run rerun` cleared the
gate and CI went green, the next pass reported `Auto-merging` and the notice was gone.
`stopwords` — `verify: human`, CI still held — stayed `In Review` with no notice, which is
correct: nobody was waiting on CI there.

**Draft pull requests were the last false `Auto-merging`.** D5.7 found that Copilot leaves its
PRs in draft when it finishes — deliberate, and correct for `verify: human`, where marking it
ready *is* the review. But a draft cannot be merged, so `verify: auto` could never close. A pass
now marks a green `verify: auto` PR ready itself, and `Auto-merging` finally implies both that CI
has passed and that a merge is possible. PR #6 is out of draft and `MERGEABLE`; PR #7 is
correctly untouched.

**A task has now gone all the way through, and the graph moved.** PR #6 was reviewed against its
own declared acceptance (`uv run pytest -q -k top && uv run ruff check .`, run locally rather
than trusted from CI) and against its task body: the limit stayed in `cli.py`, `count_words` was
left returning the unsliced list that `json-output` needs, `--top 0` prints nothing and a
negative N exits 2. Squash-merged; `Closes #1` closed `top-n` on its own.

The pass after the merge is the one that had never been observed:

```
top-n: Done                 <- closed issue
encoding-fallback: Dispatched  <- deferral released, then dispatched
json-output: Ready          <- dependency satisfied, was Blocked
dispatch: encoding-fallback
  defer json-output: file-scope-conflict (overlaps encoding-fallback)
pass complete: 1 dispatched, 3 board write(s)
```

Three separate mechanisms fired together, none previously exercised outside synthetic graphs:
dependency unblocking (`json-output` Blocked → Ready), deferral release (`encoding-fallback` had
been deferred for overlapping `top-n`), and file-scope exclusion **re-forming around the new
pair** — `json-output` was immediately deferred against `encoding-fallback`, since both touch
`cli.py` and `test_cli.py`. Copilot opened PR #8 within the minute; the next pass dispatched
nothing and wrote once. Assignment is still the lock.

**Published.** [`bioshrek/dispatchkit`](https://github.com/bioshrek/dispatchkit) is public, its
own CI is green, and `v0.1.0` is tagged — the ref the workflow template pins. The local branch was
`master` and had to be renamed to `main` first, or `ci.yml`'s `branches: [main]` would never have
fired.

The sandbox scheduler now gets as far as running dispatchkit inside Actions, which it never had
before: the log shows `PYTHONPATH: .dispatchkit/src`, `PLAN: wordfreq`, `PROJECT: 2` and a real
import from the vendored checkout. It still fails, on the one input only you can supply —
`GH_TOKEN:` is empty.

That failure also earned its keep. It arrived as a bare twelve-frame `RuntimeError` traceback:
`init` and `doctor` both caught this class of error and `tick` did not, so the failure adopters
are most likely to hit was the one presented worst. `tick --push` now reports
`dispatchkit: cannot read <repo>: …` and exits 2 — shipped as **`v0.1.1`**, since `v0.1.0` was
cut minutes earlier and published tags are not moved.

Two things were proven on the way. The sandbox is pinned to the new release through
`DISPATCHKIT_REF`, so the override the template advertises has now actually been used rather than
merely offered. And the pin test was strengthened: it asserted only that a `ref` existed, which
`ref: main` would have satisfied — the precise thing a pin prevents, since a moving branch means
every adopter runs whatever was last pushed here, in a job holding a token that can assign work.
The default must now match a version tag.

The scheduler's log now ends in one line: `dispatchkit: cannot read bioshrek/dispatchkit-sandbox:
… set the GH_TOKEN environment variable`, exit 2.

**The scheduler runs unattended.** The token is set and a pass has completed inside GitHub
Actions, which had never happened before — every previous pass was run by hand from the laptop.

```
top-n: Done              stopwords: In Review     encoding-fallback: In Review
json-output: Ready       document-flags: Blocked
dispatch: (nothing)
  defer json-output: file-scope-conflict (overlaps encoding-fallback)
pass complete: 0 dispatched, 0 board write(s)
```

**Zero board writes is the result worth reading twice.** A different machine, a different token
and a different working directory derived byte-identical state to the local passes and therefore
had nothing to correct. That is the no-stored-state claim holding across execution environments,
not merely across repeated runs — the strongest form of it that has been demonstrated.

Getting there cost one debugging cycle worth recording: the first token was wrong, `doctor`
reported `ok` for `DISPATCHKIT_TOKEN`, and the pass then failed `Bad credentials (HTTP 401)`.
GitHub never discloses a secret's value, so validity is unknowable from `doctor` by construction;
the check now says it saw the *name* only. Verify a token before setting it —
`GH_TOKEN="$PAT" gh api user` and a `projectV2` query — rather than after.

The cron is live (`7,37 * * * *`), so the sandbox now schedules itself.

**D9 landed, and `Auto-merging` is now an action.** `stopwords` went from `Auto-merging` to
`Done` with no human in the loop: `pass complete: 0 dispatched, 1 board write(s), 1 PR(s) merged`,
the squash closed issue #2, the next pass read `Done`, the one after wrote nothing.

Three things it changed that are worth knowing before touching this code:

- **Never `gh pr merge --auto`.** On a branch with no protection it merges instantly, silently,
  exit 0, no check consulted. The GraphQL mutation it wraps refuses that case, so the flag is
  strictly less safe than the API. The adapter does a direct squash merge and there is a test
  asserting `--auto` and `--admin` are both absent.
- **Branch protection is a second lock, not the first.** Every gate lives in `merge_ops`.
  `doctor`'s new `merge-gate` check reports an unprotected branch as *single-gated* rather than
  broken. The sandbox is now protected (`check` required) and `allow_auto_merge` is on.
- **`mergeable` is its own question.** Green, non-draft and mergeable are three separate fields
  and conflating them shipped a bug live. It defaults to `False`, so `UNKNOWN` waits a pass.

**D9's remaining two guardrails landed.** `verify: auto` now means what it says.

- **Acceptance ⊆ CI.** The validator refuses `verify = "auto"` unless every `&&`-clause of
  `acceptance` is covered by a command a *pull-request* workflow runs. Coverage is prefix-based,
  so a task may narrow CI (`pytest -q` covers `pytest -q -k stopword`) but never widen it. It
  failed on the live sandbox's first run: `document-flags` claimed `auto` on four `grep` commands
  CI never ran. The lesson generalises — an acceptance CI cannot run is a sign the acceptance is
  not a test.
- **Scope drift.** `merge_ops` refuses a pull request whose files are not all inside its task's
  declared `touches`. Empty `touches` refuses too: it means the question cannot be answered.

**D7 shipped too.** A dispatch that produces no pull request within `retry.stall_after` is
reclaimed by unassigning the agent — which is the whole retry, because assignment is the lock.
`attempts` is derived from the issue's assignment history rather than read from a board field
nothing ever wrote, so the budget can finally fire; it had been structurally stuck at zero since
D4. `Stuck` is now a status, not just a label, or a spent task advertises itself as `Ready`
forever. The *stale-heartbeat* half of D7 is still unbuilt, because it needs the daemon of D6.

Immediately outstanding, neither a milestone:

1. **Watch `encoding-fallback` finish.** It is `verify: human` with an open draft PR (#8);
   merging it releases `json-output`, which is deferred behind it on `cli.py`. `document-flags`
   is the remaining `verify: auto` task, and the first end-to-end test of D9's two guardrails.

2. **Watch a real pass through `watch`, then design the report.** D13 shipped the loop, the
   pooled caps and the deletion of the workflow, but deliberately not the terminal report or the
   graph watcher — both are taste decisions, and D13.1 is where they land. The report is the first
   thing anyone looks at all day; guessing at it before sitting in front of a real pass is how it
   ends up wrong. `uv run dispatchkit watch --repo bioshrek/dispatchkit-sandbox --push` is the
   command.

3. **Decide on the `[WIP]` gap.** `encoding-fallback` reported `In Review` while its PR was still
   titled `[WIP]` and the agent was still pushing to it. Nobody can review that. `draft` cannot
   separate it, since Copilot leaves finished PRs in draft too; the signal is the
   `copilot_work_finished` timeline event, which `STATE_QUERY` does not request.

## Known gaps, in the order they will bite

- **Adding a `Status` option needs a manual board migration.** `init` refuses to replace the
  options of a populated single-select, because doing so deletes the values of every item using
  it. The refusal is right, and the override is cheap: on the sandbox all 5 values were lost and
  the next pass rebuilt every one from the issues. Say so when a release adds a status, because
  an adopter who does nothing gets a scheduler that cannot write the new one.

- **`gh project field-create`'s option syntax is verified.** Checked against `gh` 2.89.0 on
  2026-09-08: `--single-select-options` is a `strings` flag and the manual's own example passes it
  comma-joined as one argument, which is what D5.5 sends. `field-list`, `field-delete`,
  `project view` and `auth status` were checked the same way and all match. Verified from
  `--help`, so it is the flag surface that is confirmed, not the responses.
- **`gh project field-list` pages at 30 by default**, and truncation reads exactly like a missing
  field. It is now sent `--limit 200`. A board with more than 200 fields would still lie, but that
  is not a board anyone has.
- **`tests/fixtures/search_issues.json` is renderer-generated, and now known to be accurate.** It
  was compared against a real response during the D5 live run: zero paths differing in either
  direction. `tests/fixtures/live_state.json` holds the recorded one alongside it — kept as well
  as, not instead of, because the live snapshot predates dispatch and swapping would lose the
  dispatched and in-review coverage. Note that neither fixture carries `checkSuites`, which D5.6
  added: the check-state tests inject suites into a copy rather than re-recording, so that shape
  is replayed from a live payload but not from a stored one.
- **The two-step-release problem is gone with the workflow.** An adopter's scheduler used to keep
  running whatever tag its workflow pinned until someone repinned it, so shipping a fix was two
  steps — the sandbox ran v0.1.1 for several passes after D9 landed. `watch` runs from whatever is
  installed on the machine, so an upgrade is an upgrade. The sandbox's own
  `.github/workflows/dispatchkit.yml` is now dead weight and should be deleted by hand, along with
  its `DISPATCHKIT_TOKEN` secret and `DISPATCHKIT_PLAN` / `DISPATCHKIT_REF` variables; nothing
  reads them, and a PAT nothing reads is a standing grant for no benefit.
- **The Copilot approval gate is not visible to `doctor`, but it can be turned off.** The
  repository setting is **Settings → Copilot → Cloud agent → "Actions workflow approval" →
  _Require approval for workflow runs_**, off by default. It is not exposed over REST — probed
  under `actions/permissions/*`, all 404 — so `doctor` cannot report it and `init` cannot set it,
  and each adopter must flip it by hand. It is *not* the fork-PR setting under Actions → General,
  which is a different mechanism: `fork-pr-contributor-approval` stays `first_time_contributors`
  and changing it would not have helped.

  Disabled on the sandbox and verified end to end: a Copilot push in response to a review comment
  produced a CI run that went straight to `success` with no `action_required` and no human
  action. Both open PRs now report `COMPLETED/SUCCESS`, `top-n` resolves to `Auto-merging` and
  `stopwords` to `In Review`, and the `ci-approval-required` notice correctly stopped firing.
  Where the setting is left on, `gh run rerun` remains the only clearing move — `POST
  /actions/runs/{id}/approve` answers 403 — which is what the notice says.

  Worth being clear about what this trades: GitHub's own warning is that unreviewed Copilot code
  may gain write access or reach Actions secrets. It is the right call in a sandbox. Before
  recommending it generally, note that since D13 there is no scheduler secret to expose at all.
  The unguarded case is a Copilot PR that edits `.github/workflows/`, which then runs unreviewed —
  which is why those paths are inside the blast-radius fence.
- **`doctor` cannot see branch protection or the merge queue.** Both are D9 prerequisites and both
  belong in the check set; they were left out because nothing consumes them yet.
- **`plan:*` labels are created by `apply`, not by `init`.** `init` creates only the labels that
  do not depend on a graph. Correct, but worth knowing when a board looks half-configured.
- **The config moved to `.github/dispatchkit.toml`.** A root `dispatchkit.toml` is still read, and
  announced when it is used. Both locations are inside the blast-radius fence.

## Decisions taken, so they are not relitigated

- **Extract before D6**, because the local daemon adds a second install target and because
  adoption pressure changes the wire format — cheaper while no issues exist.
- **Harden before the live run, not during it.** D5.5 pulled the version key, the configurable
  paths and the on-ramp forward rather than discovering them mid-trial. All three were cheap
  exactly once: while no issue, no board and no adopter existed.
- **Renamed to `dispatchkit`** in both product and distribution: `taskflow` on PyPI is
  OpenStack's, at 6.4.0.
- **Concurrency caps stay per-repo**, not per-owner. Consequence: the state query stays
  repo-driven (`repository.issues`) rather than board-driven (`ProjectV2.items`), and an adopter
  with several repos gets several independent caps.
- **Distribution is a PyPI package, and only that.** It was "a reusable workflow first" on the
  principle that the unit of distribution should match the unit of execution. The principle stood
  and the conclusion flipped at D13: the unit of execution is a command on a workstation, so
  `uvx dispatchkit` is the whole install story.
- **This repo is its own scratch repo.** Live trials leave debris here, where debris is on-brand,
  rather than in a real project — and the blast-radius fence already forces `verify: human` for
  anything touching workflows or config, so a buggy dispatcher cannot merge its own broken fix.

## Roadmap

Ordered, with the reasoning that put each where it is. Numbers are the design's own: the local
daemon stays **D6**, deferred rather than renumbered, and alerting stays **D8**. New work starts
at D10.

Two of these open with a probe rather than an implementation, because the last two design claims
that went unprobed — auto-merge semantics, and "green" meaning the task's own acceptance passed —
were both wrong.

| # | Milestone | Why here |
|---|---|---|
| **D10** | Plan-authoring contract (skill + doc) | Cheapest real leverage, no new machinery. There is currently *no* document teaching the format — only examples and `validate`'s error messages — yet the intended workflow is that an agent writes the graph. |
| **D11** | `doctor` completeness, then a gated `init` | *Shipped.* Step one shipped value alone, as expected. "Interactive" resolved to a postcondition rather than a prompt: `init` does its idempotent work, re-reads the tree it leaves behind, and gates on the local checks — a prompt would be untestable and would ask at the moment the user has least context. The Copilot workflow-approval toggle still cannot be gated; it has no API, so it stays asserted by a human. |
| **D12** | Org + GitHub App auth | Opens with a probe: can an App installation token assign Copilot? If it cannot, the milestone buys nothing. Org-first, not org-only: the sandbox is a user repo. |
| **D8** | Alerting, transition-only | Now has something true to say: D7 produces `Stuck`, the first state worth waking someone for. |
| **D6** | Local runner daemon | Deferred by choice. Largest new surface, the only place per-task `model`/`effort` could live, and the consumer of the `dispatch:local` label that nothing reads today. It also completes D7, whose stale-heartbeat reclaim has no daemon to reclaim from. |

**D10 before D11** because a correctly configured repo that emits malformed plans still fails, and
the authoring contract costs least. **D6 last** because it is the only item adding a long-running
component, and a workstation vanishing mid-task is the hardest test of D7's reclaim.

Smaller items, still worth doing, not milestones:

- re-record `search_issues.json` from a real `gh api graphql` response
- `dispatchkit doctor --json`, so a workflow can gate on it
- `dispatchkit init`: create the Project itself, once the owner-type question is settled
- add `copilot_work_finished` to `STATE_QUERY`, closing the `[WIP]` gap
