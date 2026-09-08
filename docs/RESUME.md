# Resume notes

Written at the end of the extraction session, for whoever picks this up next. `docs/design.md`
holds the design and the D1–D5 decision records; this file holds only what is *not* in there:
current state, the open decisions, and the exact next actions.

## Where things stand

D1–D5 are implemented and green offline. 256 tests, `ruff`, `mypy --strict`, `lint-imports` all
clean via `make check`.

| Step | What | State |
|------|------|-------|
| D1 | Task graph schema + validator | done |
| D2 | Structural lints + plan shape | done |
| D3 | `apply` (graph → issues + board), idempotent | done, offline only |
| D4 | Readiness resolver + admission | done |
| D5 | Cloud dispatch + workflow | code done; **the live end-to-end run has never happened** |
| D6–D9 | Local daemon, retry/reclaim, alerting, auto-merge | designed, unbuilt |

This repo was extracted from `~/Documents/py_repos/art_strategy` (where it lived as
`tools/dispatch/`) on the session dated 2026-09-08. The product was renamed `taskflow` →
`dispatchkit` during extraction, including the issue label and the machine-block marker.
`art_strategy` has been restored to its pre-extraction state and is intended to become adopter #1.

## Immediate next actions

1. **Create the GitHub repo and push.** No remote exists yet.
   ```sh
   gh repo create dispatchkit --private --source . --remote origin --push
   ```
   Decide public vs. private first — public gets free Actions minutes, and this is a tool, not a
   secret.

2. **Grant the local token the Projects scope.** Nothing project-related works without it:
   ```sh
   gh auth refresh -s project
   ```
   (Verified 2026-09-08: the account's token has `repo`, `read:org`, `gist`, `admin:public_key` —
   no `project`.)

3. **Create the board.** `gh project field-create` supports single-select options, and
   `field-delete` exists, so this is fully scriptable. The catch: a new project ships a built-in
   `Status` field with `Todo`/`In Progress`/`Done`, and the option names must be *ours*. Either
   delete that field and recreate it, or update its options via the `updateProjectV2Field`
   mutation — **untested, verify before relying on it**. Fields needed:

   | Field | Type | Options |
   |---|---|---|
   | `Status` | single select | `Blocked`, `Ready`, `Dispatched`, `In Review`, `Auto-merging`, `Done` |
   | `Lane` | single select | `cloud`, `local` |
   | `Verify` | single select | `auto`, `human` |
   | `Task ID` | text | — |
   | `Attempts` | number | — |

4. **Write the first plan graph** — `docs/plans/<plan>.tasks.toml`. Per the design's "synthetic
   tasks, but never no-ops" rule these must be real chores producing real diffs and real CI time.
   The obvious candidates are this repo's own backlog, below.

5. **Run the live trial by hand first**, before wiring the workflow — it uses the local `gh`
   credential, so no PAT is needed yet:
   ```sh
   uv run dispatchkit apply docs/plans/<plan>.tasks.toml --push --repo <owner>/dispatchkit --project <n>
   uv run dispatchkit tick  --plan <plan> --push --repo <owner>/dispatchkit --project <n>
   ```

6. **Then automate.** `.github/workflows/dispatchkit.yml` needs `secrets.DISPATCHKIT_TOKEN` plus
   repo variables `DISPATCHKIT_PLAN` and `DISPATCHKIT_PROJECT`. The token must be a PAT:
   `GITHUB_TOKEN` can neither assign the coding agent nor write a user-level Project. Set it with
   a prompt, never as an argv: `gh secret set DISPATCHKIT_TOKEN`.

## Known gaps, in the order they will bite

- **`tests/fixtures/search_issues.json` is renderer-generated, not recorded.** It is
  GraphQL-shaped but has never been compared against a real response. The live run's first job is
  to re-record it. Until then, `GhCli`'s subprocess paths — the Project field/option lookups,
  `item-add`, `item-edit`, `assign_agent` — are entirely unexecuted code.
- **The machine block has no version key.** It is a wire format written into other people's
  issues. Adding `v: 1` costs one key now and a migration later. Do it before any issue exists in
  the wild. (Decided during extraction; not yet implemented.)
- **The blast-radius fence is hardcoded** to `.github/workflows/`, `dispatch.toml` and
  `docs/plans/*.tasks.toml`. For adopters it must be config — and note the middle path still says
  `dispatch.toml`, which this repo's config no longer is. The fence therefore currently does not
  cover its own config file. Fix when D9 lands, or sooner.
- **Config location and graph path are conventions, not settings.** `dispatchkit.toml` at the repo
  root claims a name in somebody else's tree; `.github/dispatchkit.toml` is the better home.
- **A single-select value with no matching option falls back to `--text`**, which `gh` will reject
  confusingly. `GhCli.set_project_field` should fail with a message naming the field and the
  missing option. This *will* happen during board bootstrap.
- **No `init` or `doctor` command.** Both were agreed as the adoption on-ramp: `init` creates the
  board, fields, labels, workflow and config idempotently; `doctor` checks scopes, agent
  availability, board fields, labels and branch protection, and prints what is missing. `doctor`
  is also the live tier's assertion set.

## Decisions taken, so they are not relitigated

- **Extract before D6**, because the local daemon adds a second install target and because
  adoption pressure changes the wire format — cheaper while no issues exist.
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

- add the `v: 1` machine-block version key and its compatibility rule
- make the blast-radius fence and graph path configurable
- `GhCli.set_project_field`: fail loudly on a missing single-select option
- `dispatchkit doctor`
- `dispatchkit init`
- move the config to `.github/dispatchkit.toml`
