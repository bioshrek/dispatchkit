# AGENTS.md

Guidance for coding agents working in this repo.

## What this is

`dispatchkit` turns a reviewed, dependency-ordered task graph into GitHub issues and hands each
one to a coding agent when its prerequisites are actually done. Read
[docs/design.md](docs/design.md) before making structural changes — it carries the full design and
a decision record per deliverable (D1–D9), which is where the *why* lives.

The three properties everything else serves:

1. **Assignment is the lock.** Ready ⇒ open, unassigned, all dependencies closed. Dispatching a
   task removes it from the ready set, so concurrent passes converge with no lease or lockfile.
2. **There is no stored state.** `Status` is recomputed from the issues every pass and printed,
   never written back. The Project board that used to hold it was retired at D14 precisely
   because it was the only artifact here that could go stale. Never introduce a side table, a
   cache, a state file, or a `status:*` label.
3. **Zero runtime dependencies.** `src/dispatchkit` is pure standard library, enforced by
   `tests/test_stdlib_only.py`. The scheduler is `dispatchkit watch`, running on a person's
   workstation under their own credential, and it can assign work; no third-party code belongs
   in it.

## Ground rules

**Ports and adapters** — `github.py` defines the `GitHubApi` port and the operation types;
`gh_cli.py` and `workstation_cli.py` are the only modules allowed to shell out — the first
for GitHub, the second for git and the local agent. Planners (`apply.py`, `tick.py`,
`resolve.py`) are pure: state in, operations out. If a use case needs a new capability, add a
method to the port and implement it in the adapter — never call `subprocess` from a planner.
`uv run lint-imports` enforces the layering.

**Value objects over primitives** — `model.py` is the pattern: small frozen
(`@dataclass(frozen=True, slots=True)`) types (`TaskId`, `Lane`, `Verify`, `Dependency`) that make
illegal states unrepresentable. Prefer a new value object over passing another bare `str` across a
boundary.

**TDD, without exception** — every deliverable here was built test-first against synthetic graphs
and recorded API payloads, so tests are free and offline. For new behaviour: write the failing
test first and confirm it fails for the right reason, then implement. For a bug: reproduce it in a
test first, then fix without touching the test. Record any non-obvious tradeoff or root-caused bug
as a decision note in `docs/design.md`, following the existing per-deliverable entries.

**Idempotency is proven, not asserted** — the standard for any new mutating behaviour is the
convergence test: apply the plan to the in-memory double, re-read, re-plan, and require the second
plan to be empty. See `tests/test_apply.py` and `tests/test_tick.py`.

## Tests and network policy

Tiered by pytest marker: `unit` (pure), `replay` (recorded payloads), `live` (deselected by
default via `addopts`). `tests/conftest.py` monkeypatches `socket.socket` to raise for everything
not marked `live`, so the offline guarantee is enforced rather than trusted.

Never add a test that makes a live API call outside the `live` marker. Any CLI invocation in a
test must pass `--state` (a recorded snapshot) rather than `--push`.

```sh
make check   # pytest, ruff, mypy --strict, lint-imports
```

## Security rules that are not negotiable

- Issue bodies are attacker-influencable. The machine block is parsed by the closed grammar in
  `block.py` — never a YAML loader, never `eval`, never a regex that accepts unknown keys.
- Every `gh` call is an argv list. No `shell=True`, no string interpolation into a command.
- The scheduler holds no token of its own. It runs on `gh`'s credential, so nothing may write one
  anywhere, and nothing may ask an adopter to install one.
- Only pull requests mutate the tree. No command in `dispatchkit` pushes to a branch.

## Naming

The product, the package, the CLI, the issue label and the machine-block marker are all
`dispatchkit`. Keep them in sync — the label and the marker are a wire format written into other
people's issues.
