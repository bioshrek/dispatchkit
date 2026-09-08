"""`dispatch` — validate, apply and run task graphs (D1–D5).

    uv run dispatchkit validate docs/plans/<plan>.tasks.toml
    uv run dispatchkit apply    docs/plans/<plan>.tasks.toml [--push]
    uv run dispatchkit resolve  --state state.json --plan <plan>
    uv run dispatchkit tick     --plan <plan> [--repo o/n --project N --push]

Exit codes: 0 success, 1 the graph is invalid (or `--strict` lints tripped),
2 the command could not be carried out (unreadable file, missing routing
options). Keeping "couldn't run it" distinct from "ran it and the graph is
wrong" matters once this is called from a workflow, where the two want
different responses.

`apply` is a dry run unless `--push` is given, and on a dry run no client is
constructed at all — reconciling a graph into a real tracker is not something
to do by accident. `resolve` is read-only in every mode: it reports what the
scheduler sees and what it would do, and nothing else.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dispatchkit.apply import execute_plan, plan_apply
from dispatchkit.config import DEFAULT_PATH, load_config
from dispatchkit.errors import GraphError, GraphIssue
from dispatchkit.gh_cli import GhCli, parse_state
from dispatchkit.github import RepoState
from dispatchkit.lints import lint_graph
from dispatchkit.metrics import plan_shape
from dispatchkit.model import TaskGraph, TaskId
from dispatchkit.parse import parse_graph
from dispatchkit.resolve import admit, build_items, reconcile_ops, resolve
from dispatchkit.tick import execute_tick, plan_tick, summarise
from dispatchkit.validate import validate_graph

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNREADABLE = 2


def plan_name(path: Path) -> str:
    """`docs/plans/refactor.tasks.toml` → `refactor`."""
    return path.name.removesuffix(".toml").removesuffix(".tasks")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dispatch", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check a task graph file")
    validate.add_argument("graph", type=Path)
    validate.add_argument(
        "--strict",
        action="store_true",
        help="fail on structural lints, not just on invalid graphs",
    )

    apply_cmd = sub.add_parser("apply", help="reconcile a task graph into issues and a project")
    apply_cmd.add_argument("graph", type=Path)
    apply_cmd.add_argument(
        "--push", action="store_true", help="perform the operations (default: dry run)"
    )
    apply_cmd.add_argument(
        "--verbose", action="store_true", help="print the full issue body for each operation"
    )
    apply_cmd.add_argument("--repo", help="owner/name; required with --push")
    apply_cmd.add_argument("--project", type=int, help="Project (v2) number; required with --push")

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
        default=DEFAULT_PATH,
        help="scheduler config (default: dispatchkit.toml)",
    )

    tick_cmd = sub.add_parser("tick", help="run one scheduler pass (dispatch included)")
    tick_cmd.add_argument("--plan", required=True, help="plan name to run")
    tick_cmd.add_argument("--state", type=Path, help="dry run against a recorded snapshot")
    tick_cmd.add_argument("--repo", help="owner/name; required with --push")
    tick_cmd.add_argument("--project", type=int, help="Project (v2) number; required with --push")
    tick_cmd.add_argument(
        "--push", action="store_true", help="perform the operations (default: dry run)"
    )
    tick_cmd.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_PATH,
        help="scheduler config (default: dispatchkit.toml)",
    )

    args = parser.parse_args(argv)
    if args.command == "tick":
        return _tick(args)
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
        print("re-run with --push --repo owner/name --project N to apply")
        return EXIT_OK

    if not args.repo or args.project is None:
        print("dispatch: --push requires --repo owner/name and --project N", file=sys.stderr)
        return EXIT_UNREADABLE

    api = GhCli(repo=args.repo, project=args.project)
    plan = plan_apply(graph, api.fetch_state(plan=graph.plan), specs)
    result = execute_plan(plan, api)
    print(
        f"applied plan `{graph.plan}`: {result.created} created, {result.updated} updated, "
        f"{result.project_items} project items, {result.fields_set} fields set"
    )
    for notice in plan.notices:
        print(f"NOTE {notice}")
    return EXIT_OK


def _resolve(args: argparse.Namespace) -> int:
    path: Path = args.state
    state = _load_state(path)
    if isinstance(state, int):
        return state

    try:
        config = load_config(args.config)
    except GraphError as exc:
        return _report(args.config, list(exc.issues))

    items, notices = build_items(state, plan=args.plan)
    for notice in notices:
        print(f"NOTE {notice}")
    if not items:
        print(f"no issues found for plan `{args.plan}` in {path}")
        return EXIT_OK

    statuses = resolve(items)
    width = max(len(task.id) for task in items)
    for task in items:
        print(f"  #{task.number:<4} {task.id:<{width}}  {statuses[task.id].value}")

    plan = admit(items, statuses, config)
    print("admit: " + (" ".join(plan.admitted) if plan.admitted else "(nothing ready)"))
    for deferral in plan.deferred:
        print(f"  defer {deferral}")

    ops = reconcile_ops(items, statuses)
    if not ops:
        print("board is up to date")
    for op in ops:
        print(f"  write {op.task_id} {op.field_name}={op.value}")
    return EXIT_OK


def _tick(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except GraphError as exc:
        return _report(args.config, list(exc.issues))

    if args.push:
        if not args.repo or args.project is None:
            print("dispatch: --push requires --repo owner/name and --project N", file=sys.stderr)
            return EXIT_UNREADABLE
        api = GhCli(repo=args.repo, project=args.project)
        state = api.fetch_state(plan=args.plan)
    elif args.state is not None:
        loaded = _load_state(args.state)
        if isinstance(loaded, int):
            return loaded
        api, state = None, loaded
    else:
        print("dispatch: a dry run needs --state; use --push to run for real", file=sys.stderr)
        return EXIT_UNREADABLE

    plan = plan_tick(state, plan=args.plan, config=config)
    for line in summarise(plan):
        print(line)

    if api is None:
        print(f"dry run: {len(plan.operations)} operation(s); re-run with --push to apply")
        return EXIT_OK

    result = execute_tick(plan, api)
    print(f"pass complete: {result.dispatched} dispatched, {result.reconciled} board write(s)")
    return EXIT_OK


def _load_state(path: Path) -> RepoState | int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"dispatch: cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    return parse_state(payload)


def _load(path: Path) -> TaskGraph | int:
    """Parse and validate, or return the exit code the caller should use."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"dispatch: graph file not found: {path}", file=sys.stderr)
        return EXIT_UNREADABLE
    except OSError as exc:
        print(f"dispatch: cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    try:
        graph = parse_graph(text, plan=plan_name(path))
    except GraphError as exc:
        return _report(path, list(exc.issues))

    issues = validate_graph(graph)
    if issues:
        return _report(path, issues)
    return graph


def _read_specs(graph: TaskGraph, path: Path) -> dict[TaskId, str] | int:
    """Resolve `body_file` paths relative to the graph file's own directory."""
    specs: dict[TaskId, str] = {}
    for task in graph.tasks:
        if task.body_file is None:
            continue
        try:
            specs[task.id] = (path.parent / task.body_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"dispatch: cannot read body_file for `{task.id}`: {exc}", file=sys.stderr)
            return EXIT_UNREADABLE
    return specs


def _report(path: Path, issues: Sequence[GraphIssue]) -> int:
    print(f"FAIL {path}: {len(issues)} issue(s)", file=sys.stderr)
    for issue in issues:
        print(f"  {issue}", file=sys.stderr)
    return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
