"""`dispatchkit` — validate, apply and run task graphs (D1–D5.5).

    uv run dispatchkit doctor   [--repo o/n]
    uv run dispatchkit init     [--repo o/n]
    uv run dispatchkit validate docs/plans/<plan>.tasks.toml
    uv run dispatchkit apply    docs/plans/<plan>.tasks.toml [--push --repo o/n]
    uv run dispatchkit resolve  --state state.json --plan <plan>
    uv run dispatchkit watch    --repo o/n --push [--once --interval 60]

Exit codes: 0 success, 1 the graph is invalid (or `--strict` lints tripped, or
`doctor` found something wrong), 2 the command could not be carried out
(unreadable file, missing routing options). Keeping "couldn't run it" distinct
from "ran it and the answer is no" matters once this is called from a
workflow, where the two want different responses.

`apply` and `watch` are dry runs unless `--push` is given, and on a dry run no
client is constructed at all — reconciling a graph into a real tracker is not
something to do by accident. `init` needs no such gate since D14: it writes
files that do not exist and creates labels, both idempotent and neither
notifying anyone. `resolve` and `doctor` are read-only in every mode.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from dispatchkit.apply import execute_plan, plan_apply
from dispatchkit.config import SchedulerConfig, find_config, load_config
from dispatchkit.doctor import (
    Check,
    Diagnostics,
    LocalFacts,
    check_local,
    check_remote,
    healthy,
    summarise,
)
from dispatchkit.errors import GraphError, GraphIssue
from dispatchkit.gh_cli import GhCli, parse_state
from dispatchkit.github import RepoState
from dispatchkit.init import WORKFLOW_PATH, execute_init, plan_init
from dispatchkit.init import summarise as summarise_init
from dispatchkit.lints import lint_graph
from dispatchkit.metrics import plan_shape
from dispatchkit.model import TaskGraph, TaskId
from dispatchkit.parse import parse_graph
from dispatchkit.resolve import admit, build_items, resolve
from dispatchkit.tick import execute_tick, plan_tick
from dispatchkit.tick import summarise as summarise_tick
from dispatchkit.validate import (
    ci_commands,
    triggers_on_pull_request,
    validate_acceptance,
    validate_graph,
)

#: Seconds between passes. Long enough not to spend an API rate limit on an
#: idle backlog, short enough that a pull request going green is picked up
#: while the person who is watching still has it in mind.
DEFAULT_INTERVAL = 60

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNREADABLE = 2


def plan_name(path: Path) -> str:
    """`docs/plans/refactor.tasks.toml` → `refactor`."""
    return path.name.removesuffix(".toml").removesuffix(".tasks")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dispatchkit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check a task graph file")
    validate.add_argument("graph", type=Path)
    validate.add_argument(
        "--strict",
        action="store_true",
        help="fail on structural lints, not just on invalid graphs",
    )

    apply_cmd = sub.add_parser("apply", help="reconcile a task graph into GitHub issues")
    apply_cmd.add_argument("graph", type=Path)
    apply_cmd.add_argument(
        "--push", action="store_true", help="perform the operations (default: dry run)"
    )
    apply_cmd.add_argument(
        "--verbose", action="store_true", help="print the full issue body for each operation"
    )
    apply_cmd.add_argument("--repo", help="owner/name; required with --push")

    resolve_cmd = sub.add_parser(
        "resolve", help="derive task status from a recorded issue snapshot (read-only)"
    )
    resolve_cmd.add_argument(
        "--state", type=Path, required=True, help="recorded GraphQL state payload"
    )
    resolve_cmd.add_argument("--plan", required=True, help="plan name to resolve")
    resolve_cmd.add_argument(
        "--config",
        type=Path,
        default=None,
        help="scheduler config (default: .github/dispatchkit.toml, then dispatchkit.toml)",
    )

    watch_cmd = sub.add_parser(
        "watch", help="the scheduler: run a pass, wait, repeat (every plan in the repository)"
    )
    watch_cmd.add_argument(
        "--once", action="store_true", help="run a single pass and exit, rather than looping"
    )
    watch_cmd.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"seconds to wait between passes (default: {DEFAULT_INTERVAL})",
    )
    watch_cmd.add_argument("--state", type=Path, help="dry run against a recorded snapshot")
    watch_cmd.add_argument("--repo", help="owner/name; required with --push")
    watch_cmd.add_argument(
        "--push", action="store_true", help="perform the operations (default: dry run)"
    )
    watch_cmd.add_argument(
        "--config",
        type=Path,
        default=None,
        help="scheduler config (default: .github/dispatchkit.toml, then dispatchkit.toml)",
    )

    doctor_cmd = sub.add_parser(
        "doctor", help="check whether this repository can run a scheduler pass"
    )
    doctor_cmd.add_argument("--root", type=Path, default=Path(), help="repository root")
    doctor_cmd.add_argument(
        "--repo", help="owner/name; also checks the repository itself when given"
    )
    doctor_cmd.add_argument("--config", type=Path, default=None, help="scheduler config")

    init_cmd = sub.add_parser("init", help="create the labels, config, workflow and plans dir")
    init_cmd.add_argument("--root", type=Path, default=Path(), help="repository root")
    init_cmd.add_argument("--repo", help="owner/name; creates the labels too when given")
    init_cmd.add_argument("--config", type=Path, default=None, help="scheduler config")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "init":
        return _init(args)
    if args.command == "watch":
        return _watch(args)
    if args.command == "apply":
        return _apply(args)
    if args.command == "resolve":
        return _resolve(args)
    return _validate(args)


def _validate(args: argparse.Namespace) -> int:
    path: Path = args.graph
    graph = _load(path)
    if isinstance(graph, int):
        return graph

    warnings = lint_graph(graph)
    if warnings and args.strict:
        print(f"FAIL {path}: {len(warnings)} lint(s), and --strict was requested", file=sys.stderr)
        for warning in warnings:
            print(f"  {warning}", file=sys.stderr)
        return EXIT_INVALID

    print(f"OK  {path}: plan `{graph.plan}`, {len(graph.tasks)} tasks")
    print("    " + plan_shape(graph).summary())
    print("    dispatch order: " + " ".join(graph.topological_order()))
    for warning in warnings:
        print(f"WARN {warning}")
    return EXIT_OK


def _apply(args: argparse.Namespace) -> int:
    path: Path = args.graph
    graph = _load(path)
    if isinstance(graph, int):
        return graph

    specs = _read_specs(graph, path)
    if isinstance(specs, int):
        return specs

    if not args.push:
        plan = plan_apply(graph, RepoState(()), specs)
        print(
            f"dry run: plan `{graph.plan}`, {len(plan.operations)} operations "
            "against an empty repository"
        )
        for operation in plan.operations:
            print(f"  {type(operation).__name__} {operation.task_id}")
            body = getattr(operation, "body", None)
            if args.verbose and body is not None:
                print("".join(f"    | {line}\n" for line in body.splitlines()), end="")
        for notice in plan.notices:
            print(f"NOTE {notice}")
        print("re-run with --push --repo owner/name to apply")
        return EXIT_OK

    if not args.repo:
        print("dispatchkit: --push requires --repo owner/name", file=sys.stderr)
        return EXIT_UNREADABLE

    api = GhCli(repo=args.repo)
    plan = plan_apply(graph, api.fetch_state(), specs)
    result = execute_plan(plan, api)
    print(f"applied plan `{graph.plan}`: {result.created} created, {result.updated} updated")
    for notice in plan.notices:
        print(f"NOTE {notice}")
    return EXIT_OK


def _resolve(args: argparse.Namespace) -> int:
    path: Path = args.state
    state = _load_state(path)
    if isinstance(state, int):
        return state

    config = _config(args)
    if isinstance(config, int):
        return config

    items, notices = build_items(state, plan=args.plan)
    for notice in notices:
        print(f"NOTE {notice}")
    if not items:
        print(f"no issues found for plan `{args.plan}` in {path}")
        return EXIT_OK

    statuses = resolve(items)
    width = max(len(task.id) for task in items)
    for task in items:
        print(f"  #{task.number:<4} {task.id:<{width}}  {statuses[task.ref].value}")

    plan = admit(items, statuses, config)
    handed = " ".join(str(ref) for ref in plan.admitted)
    print("admit: " + (handed if plan.admitted else "(nothing ready)"))
    for deferral in plan.deferred:
        print(f"  defer {deferral}")

    return EXIT_OK


def _watch(args: argparse.Namespace) -> int:
    """The scheduler. One pass, wait, repeat — `--once` stops after the first.

    There is no cron and no workflow behind this since D13: the loop runs in a
    terminal a human is sitting at, which is what lets it hold `gh`'s keychain
    credential instead of a broad classic PAT in Actions secrets. The accepted
    cost is that the graph only moves while this is running.
    """
    config = _config(args)
    if isinstance(config, int):
        return config

    if args.interval < 1:
        print("dispatchkit: --interval must be at least 1 second", file=sys.stderr)
        return EXIT_UNREADABLE

    if args.push:
        if not args.repo:
            print("dispatchkit: --push requires --repo owner/name", file=sys.stderr)
            return EXIT_UNREADABLE
        api = GhCli(repo=args.repo)
    elif args.state is not None:
        api = None
    else:
        print("dispatchkit: a dry run needs --state; use --push to run for real", file=sys.stderr)
        return EXIT_UNREADABLE

    # A file cannot change under the loop, so looping over one would reprint
    # the same pass forever. Saying so beats silently doing something else.
    once = args.once or api is None
    if api is None and not args.once:
        print("NOTE a recorded snapshot cannot change, so this is a single pass")

    # The interrupt is caught around the whole loop, not just the wait:
    # Ctrl-C is likeliest to land in the seconds a pass is talking to GitHub.
    # Stopping the scheduler is how you stop the scheduler, and a twelve-frame
    # traceback would report the ordinary way of using it as a crash. Nothing
    # needs unwinding, because a half-finished pass leaves only the operations
    # it already sent, and the next pass re-derives everything.
    try:
        while True:
            code = _pass(args, config, api)
            if code != EXIT_OK or once:
                return code
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nwatch: stopped")
        return EXIT_OK


def _pass(args: argparse.Namespace, config: SchedulerConfig, api: GhCli | None) -> int:
    """One scheduler pass, from a fresh read.

    Every decision is recomputed here. The process may hold a snapshot for
    rendering, but nothing survives between passes — which is what keeps the
    convergence standard true across a restart as well as across a re-run.
    """
    if api is not None:
        try:
            state = api.fetch_state()
        except RuntimeError as exc:
            print(f"dispatchkit: cannot read {args.repo}: {exc}", file=sys.stderr)
            return EXIT_UNREADABLE
    else:
        loaded = _load_state(args.state)
        if isinstance(loaded, int):
            return loaded
        state = loaded

    plan = plan_tick(state, config=config, now=datetime.now(UTC))
    for line in summarise_tick(plan):
        print(line)

    if api is None:
        print(f"dry run: {len(plan.operations)} operation(s); re-run with --push to apply")
        return EXIT_OK

    result = execute_tick(plan, api)
    line = f"pass complete: {result.dispatched} dispatched"
    if result.readied:
        line += f", {result.readied} PR(s) marked ready"
    if result.merged:
        line += f", {result.merged} PR(s) merged"
    if result.reclaimed:
        line += f", {result.reclaimed} stalled dispatch(es) reclaimed"
    print(line)
    for notice in result.refused:
        print(f"NOTE {notice}")
    return EXIT_OK


def _config(args: argparse.Namespace) -> SchedulerConfig | int:
    return _load_config(args.config if args.config is not None else find_config())


def _load_config(path: Path) -> SchedulerConfig | int:
    """Load the scheduler config, or return the exit code the caller should use."""
    if path.name == "dispatchkit.toml" and path.parent.name != ".github" and path.exists():
        print(f"NOTE the config is read from {path}; `.github/{path.name}` is its home")
    try:
        return load_config(path)
    except GraphError as exc:
        return _report(path, list(exc.issues))


def _facts(args: argparse.Namespace) -> LocalFacts | int:
    """What the working tree provides, read once for `doctor` and `init`."""
    root: Path = args.root
    config_path = args.config if args.config is not None else find_config(root)
    config = _load_config(config_path)
    if isinstance(config, int):
        return config

    workflow = root / WORKFLOW_PATH
    plans = root / config.plans
    return LocalFacts(
        config_path=config_path,
        config_exists=config_path.exists(),
        workflow_path=workflow,
        workflow_exists=workflow.exists(),
        plans=plans,
        plans_exists=plans.is_dir(),
        workflow_text=workflow.read_text(encoding="utf-8") if workflow.exists() else "",
        vendored=(root / "src" / "dispatchkit").is_dir(),
    )


def _doctor(args: argparse.Namespace) -> int:
    facts = _facts(args)
    if isinstance(facts, int):
        return facts

    checks = check_local(facts)
    if args.repo:
        api = GhCli(repo=args.repo)
        try:
            variables, secrets = api.workflow_inputs()
            diagnostics = Diagnostics(
                scopes=api.token_scopes(),
                agent_available=api.agent_available(),
                labels=api.fetch_labels(),
                variables=variables,
                secrets=secrets,
                protected_branch=api.branch_protected(),
            )
        except RuntimeError as exc:
            # Every failure `doctor` exists to name arrives as a non-zero `gh`
            # exit: a repository that does not exist, or no credential at all.
            # Reporting it is the answer, so it is a failed check and not a
            # traceback — but the repository went unread, so the verdict is
            # "could not be carried out" rather than "unhealthy".
            checks = (_unreachable(exc),) + checks
            for line in summarise(checks):
                print(line)
            return EXIT_UNREADABLE
        checks = check_remote(diagnostics) + checks
    else:
        print("NOTE the repository was not inspected; pass --repo owner/name to check it")

    for line in summarise(checks):
        print(line)
    return EXIT_OK if healthy(checks) else EXIT_INVALID


def _unreachable(exc: RuntimeError) -> Check:
    return Check(
        name="repository",
        ok=False,
        detail=str(exc),
        remedy=(
            "check --repo names a repository you can read, and that you are logged "
            "in: `gh auth status`, then `gh auth login`"
        ),
    )


def _init(args: argparse.Namespace) -> int:
    facts = _facts(args)
    if isinstance(facts, int):
        return facts

    api = GhCli(repo=args.repo) if args.repo else None
    try:
        labels = api.fetch_labels() if api is not None else ()
    except RuntimeError as exc:
        print(f"dispatchkit: cannot read {args.repo}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    plan = plan_init(labels, facts)

    for line in summarise_init(plan):
        print(line)
    # stderr is unbuffered, so without this a failure below overtakes the plan
    # it failed on, and the output reads back to front.
    sys.stdout.flush()

    try:
        result = execute_init(plan, api)
    except RuntimeError as exc:
        # Stopping part-way is safe to report plainly: every operation is
        # idempotent, so the remedy is always to re-run.
        print(f"dispatchkit: init stopped: {exc}; re-run once fixed", file=sys.stderr)
        return EXIT_UNREADABLE

    line = f"init: {result.files} file(s), {result.directories} directory(ies)"
    if api is None:
        line += "; no --repo, so the labels were not created"
    else:
        line += f", {result.labels} label(s)"
    print(line)
    return EXIT_OK


def _load_state(path: Path) -> RepoState | int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"dispatchkit: cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    return parse_state(payload)


def _load(path: Path) -> TaskGraph | int:
    """Parse and validate, or return the exit code the caller should use."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"dispatchkit: graph file not found: {path}", file=sys.stderr)
        return EXIT_UNREADABLE
    except OSError as exc:
        print(f"dispatchkit: cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    try:
        graph = parse_graph(text, plan=plan_name(path))
    except GraphError as exc:
        return _report(path, list(exc.issues))

    issues = validate_graph(graph)
    # `verify = "auto"` is a claim about *this repository's* CI, so it can only
    # be checked against the workflows next to the graph file.
    issues += validate_acceptance(graph, _ci_commands(path))
    if issues:
        return _report(path, issues)
    return graph


def _ci_commands(graph_path: Path) -> tuple[str, ...]:
    """Every command this repository's pull-request workflows run.

    The repository root is found by walking up from the graph file until a
    `.github/workflows` appears, because the graph's own location is a
    configurable path and not a reliable anchor.
    """
    for parent in [graph_path.parent, *graph_path.parents]:
        workflows = parent / ".github" / "workflows"
        if workflows.is_dir():
            break
    else:  # pragma: no cover - the loop above always terminates at the root
        return ()
    if not workflows.is_dir():
        return ()

    commands: list[str] = []
    for file in sorted(workflows.iterdir()):
        if file.suffix not in {".yml", ".yaml"}:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        if triggers_on_pull_request(text):
            commands += ci_commands(text)
    return tuple(commands)


def _read_specs(graph: TaskGraph, path: Path) -> dict[TaskId, str] | int:
    """Resolve `body_file` paths relative to the graph file's own directory."""
    specs: dict[TaskId, str] = {}
    for task in graph.tasks:
        if task.body_file is None:
            continue
        try:
            specs[task.id] = (path.parent / task.body_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"dispatchkit: cannot read body_file for `{task.id}`: {exc}", file=sys.stderr)
            return EXIT_UNREADABLE
    return specs


def _report(path: Path, issues: Sequence[GraphIssue]) -> int:
    print(f"FAIL {path}: {len(issues)} issue(s)", file=sys.stderr)
    for issue in issues:
        print(f"  {issue}", file=sys.stderr)
    return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
