# Resume notes

Written for whoever picks this up next. `docs/design.md` holds the design and the D1–D5.5
decision records; this file holds only what is *not* in there: current state, the open decisions,
and the exact next actions.

## Where things stand

D1–D5.5 are implemented and green offline. 370 tests, `ruff`, `mypy --strict`, `lint-imports` all
clean via `make check`.

| Step | What | State |
|------|------|-------|
| D1 | Task graph schema + validator | done |
| D2 | Structural lints + plan shape | done |
| D3 | `apply` (graph → issues + board), idempotent | done, offline only |
| D4 | Readiness resolver + admission | done |
| D5 | Cloud dispatch + workflow | code done; `doctor` now runs live, **`apply`/`tick` still never have** |
| D5.5 | Block version key, configurable paths/fence, `doctor`, `init` | done; `doctor`/`init --local` exercised live |
| D6–D9 | Local daemon, retry/reclaim, alerting, auto-merge | designed, unbuilt |

This repo was extracted from `~/Documents/py_repos/art_strategy` (where it lived as
`tools/dispatch/`) on 2026-09-08. `art_strategy` is intended to become adopter #1.

## Immediate next actions

The live trial runs against a **disposable sandbox**, not this repo:
[`bioshrek/dispatchkit-sandbox`](https://github.com/bioshrek/dispatchkit-sandbox), public,
created 2026-09-08 and checked out at `../dispatchkit-sandbox`. `tick` assigns a coding agent and
D9 will auto-merge, and none of that argv had ever been executed — a sandbox absorbs a first run
that misbehaves. Its payload is `wordfreq`, a deliberately unfinished CLI with real tests and real
CI (green in 13s), so the backlog is genuine chores rather than the no-ops the design forbids.

Already done, and the first live exercise of the adapter:

- The sandbox exists, is pushed, and its CI passes.
- `dispatchkit init --local --root ../dispatchkit-sandbox` wrote its config, workflow and plans
  directory. Files landed where the summary said they would, which is the relative-root
  double-join bug staying fixed outside the tests.
- `dispatchkit doctor --root ../dispatchkit-sandbox --repo bioshrek/dispatchkit-sandbox
  --project 1` **ran against real GitHub** and returned `FAIL board` with its remedy and exit 2 —
  diagnosing the missing scope instead of raising. The local checks all report `ok`.

1. **Grant the local token the Projects scope.** *This one needs a human: it is an interactive
   browser flow.* Nothing project-related works without it:
   ```sh
   gh auth refresh -s project
   ```
   (Verified 2026-09-08: the account's token has `repo`, `read:org`, `gist`, `admin:public_key` —
   no `project`. Note `gh`'s own hint says `-s read:project`; that is not enough, because `init`
   *writes* fields.)

2. **Create the board, then let `init` fill it in.** The Project itself still has to be created by
   hand (`gh project create`); everything after that is one command:
   ```sh
   gh project create --owner bioshrek --title dispatchkit-sandbox
   S="--root ../dispatchkit-sandbox --repo bioshrek/dispatchkit-sandbox --project <n>"
   uv run dispatchkit doctor $S   # what is missing
   uv run dispatchkit init   $S --push
   uv run dispatchkit doctor $S   # expect all `ok`
   ```

   `init` creates every field with its options, every label, and any missing file. The built-in
   `Status` field carrying `Todo`/`In Progress`/`Done` is handled: on an empty board it is deleted
   and recreated; on a populated one you get a notice naming the `field-delete` command, because
   deleting a field deletes its values and that is not a machine's call.

3. **Write the first plan graph** — `../dispatchkit-sandbox/docs/plans/<plan>.tasks.toml`. Per
   the design's "synthetic tasks, but never no-ops" rule these must be real chores producing real
   diffs and real CI time. The sandbox README lists four, chosen to be exactly that: `--top N`,
   stopword filtering, `--json` output, and an encoding fallback.

4. **Run the live trial by hand first**, before wiring the workflow — it uses the local `gh`
   credential, so no PAT is needed yet:
   ```sh
   uv run dispatchkit apply ../dispatchkit-sandbox/docs/plans/<plan>.tasks.toml --push $S
   uv run dispatchkit tick  --plan <plan> --push $S
   ```
   The first job here is to **re-record `tests/fixtures/search_issues.json`** from the real
   response, since today's is renderer-generated.

5. **Then create the real repo**, once the sandbox has proven the path end to end:
   ```sh
   gh repo create dispatchkit --public --source . --remote origin --push
   ```
   Then the same three commands from step 2 against it.

6. **Then automate.** `.github/workflows/dispatchkit.yml` needs `secrets.DISPATCHKIT_TOKEN` plus
   repo variables `DISPATCHKIT_PLAN` and `DISPATCHKIT_PROJECT`. The token must be a PAT:
   `GITHUB_TOKEN` can neither assign the coding agent nor write a user-level Project. Set it with
   a prompt, never as an argv: `gh secret set DISPATCHKIT_TOKEN`.

## Known gaps, in the order they will bite

- **`gh project field-create`'s option syntax is verified.** Checked against `gh` 2.89.0 on
  2026-09-08: `--single-select-options` is a `strings` flag and the manual's own example passes it
  comma-joined as one argument, which is what D5.5 sends. `field-list`, `field-delete`,
  `project view` and `auth status` were checked the same way and all match. Verified from
  `--help`, so it is the flag surface that is confirmed, not the responses.
- **`gh project field-list` pages at 30 by default**, and truncation reads exactly like a missing
  field. It is now sent `--limit 200`. A board with more than 200 fields would still lie, but that
  is not a board anyone has.
- **`tests/fixtures/search_issues.json` is renderer-generated, not recorded.** It is
  GraphQL-shaped but has never been compared against a real response. The live run's first job is
  to re-record it. Until then every `GhCli` subprocess path — the Project field lookups,
  `item-add`, `item-edit`, `assign_agent`, and D5.5's `fetch_board`, `create_field`,
  `delete_field`, `token_scopes` — is argv-checked but unexecuted code.
- **`doctor` cannot see branch protection or the merge queue.** Both are D9 prerequisites and both
  belong in the check set; they were left out because nothing consumes them yet.
- **`init` cannot create the Project itself.** `gh project create` needs an owner-type decision
  (user vs. org) that changes the token requirements, so it is deliberately a human's first step.
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
- **Distribution is a reusable workflow first, a PyPI package second.** The package matters for
  the D6 workstation daemon; most adopters should only ever need a workflow file.
- **This repo is its own scratch repo.** Live trials leave debris here, where debris is on-brand,
  rather than in a real project — and the blast-radius fence already forces `verify: human` for
  anything touching workflows or config, so a buggy dispatcher cannot merge its own broken fix.

## Backlog worth dispatching once D5 is proven

Real, small, independently useful — exactly what the design asks test tasks to be:

- re-record `search_issues.json` from a real `gh api graphql` response
- `dispatchkit doctor`: check branch protection and the merge queue (D9's prerequisites)
- `dispatchkit doctor --json`, so a workflow can gate on it
- `dispatchkit init`: create the Project itself, once the owner-type question is settled
- D6: the local daemon — claim, worktree, heartbeat, spend gate
