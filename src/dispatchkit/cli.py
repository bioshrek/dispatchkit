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
import shutil
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from dispatchkit.apply import execute_plan, plan_apply
from dispatchkit.config import SchedulerConfig, find_config, load_config
from dispatchkit.dispatcher import LocalDispatcher, served_lanes
from dispatchkit.doctor import (
    Check,
    Diagnostics,
    LocalFacts,
    check_cli,
    check_local,
    check_remote,
    check_runner,
    healthy,
    summarise,
)
from dispatchkit.errors import GraphError, GraphIssue
from dispatchkit.gh_cli import GhCli, parse_state
from dispatchkit.github import RepoState
from dispatchkit.init import execute_init, plan_init
from dispatchkit.init import summarise as summarise_init
from dispatchkit.lints import lint_graph
from dispatchkit.local import LocalRun
from dispatchkit.metrics import plan_shape
from dispatchkit.model import TaskGraph, TaskId
from dispatchkit.parse import parse_graph
from dispatchkit.recover import DispatcherBusy, hold_dispatcher
from dispatchkit.resolve import admit, build_items, resolve
from dispatchkit.tick import TickPlan, TickResult, execute_tick, plan_tick
from dispatchkit.tick import summarise as summarise_tick
from dispatchkit.validate import (
    ci_commands,
    triggers_on_pull_request,
    validate_acceptance,
    validate_graph,
)
from dispatchkit.watcher import GraphWatcher, Save
from dispatchkit.workstation import work_root
from dispatchkit.workstation_cli import CliWorkstation

#: Seconds between passes. Long enough not to spend an API rate limit on an
#: idle backlog, short enough that a pull request going green is picked up
#: while the person who is watching still has it in mind.
DEFAULT_INTERVAL = 60

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNREADABLE = 2

#: The one hard external dependency. Named once so `doctor` and the adapter
#: cannot disagree about what is missing.
GH = "gh"


def plan_name(path: Path) -> str:
    """`docs/plans/refactor.tasks.toml` → `refactor`."""
    return path.name.removesuffix(".toml").removesuffix(".tasks")


def declared_bases(plans: Path) -> tuple[str, ...]:
    """Every branch a plan in this directory integrates on (D16).

    Read from the plan files rather than from the repository, because that is
    where a base is declared: a branch no plan names is not one anything will
    merge into, and a branch a plan names is one even before it exists.

    A plan that will not parse is skipped rather than raised on. `doctor` is
    the command people run when things are broken, `validate` is the command
    that reports a broken plan, and taking the diagnostic down would withhold
    every other answer it had.
    """
    if not plans.is_dir():
        return ()
    bases: list[str] = []
    for file in sorted(plans.iterdir()):
        if not file.name.endswith(".tasks.toml"):
            continue
        try:
            graph = parse_graph(file.read_text(encoding="utf-8"), plan=plan_name(file))
        except (OSError, GraphError):
            continue
        if str(graph.base) not in bases:
            bases.append(str(graph.base))
    return tuple(bases)


def _merge_targets(plans: Path) -> tuple[str, ...]:
    """The branches a `verify: auto` merge could land on.

    The plans' bases, or the default branch when there are no plans yet -- so
    `doctor` on a fresh repository still says what `init` is about to need,
    rather than passing vacuously because nothing has been written down.
    """
    return declared_bases(plans) or ("main",)


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface, separated from running it.

    So a test can ask what the CLI accepts without executing anything —
    which is how the Makefile's two dead invocations were finally caught.
    """
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
        "--local",
        action="store_true",
        help="also run lane: local tasks on this machine (requires --push)",
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
    doctor_cmd.add_argument(
        "--local",
        action="store_true",
        help="also check that the local lane's runner is installed on this machine",
    )
    doctor_cmd.add_argument("--config", type=Path, default=None, help="scheduler config")

    init_cmd = sub.add_parser("init", help="create the labels, the config and the plans dir")
    init_cmd.add_argument("--root", type=Path, default=Path(), help="repository root")
    init_cmd.add_argument("--repo", help="owner/name; creates the labels too when given")
    init_cmd.add_argument("--config", type=Path, default=None, help="scheduler config")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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

    specs = _read_specs(graph, path)
    if isinstance(specs, int):
        return specs

    warnings = lint_graph(graph, specs=specs)
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

    if getattr(args, "local", False) and api is None:
        print("dispatchkit: --local requires --push", file=sys.stderr)
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
        with _dispatcher(args, config, api) as local:
            if once:
                return _pass(args, config, api, local=local)[0]

            previous: TickPlan | None = None
            number = 0
            watcher = GraphWatcher(config.plans, sleep=time.sleep)
            while True:
                number += 1
                code, plan = _pass(args, config, api, local=local, since=previous, number=number)
                if code == EXIT_OK:
                    previous = plan
                else:
                    # A failed pass is a wait, not an exit (D6.6). The
                    # scheduler is meant to be left running, and the failures
                    # it meets overnight are transient by nature; there is no
                    # stored state to be left inconsistent, so the next pass
                    # re-derives everything. `previous` is deliberately not
                    # advanced: the next report must be a full one rather than
                    # a diff against a pass that did not finish.
                    previous = None
                saved = watcher.wait(args.interval)
                if saved:
                    for line in _saved(saved, config):
                        print(line)
    except DispatcherBusy as busy:
        print(f"dispatchkit: {busy}", file=sys.stderr)
        return EXIT_UNREADABLE
    except KeyboardInterrupt:
        # The local run, if there is one, dies with the process. Its mark
        # stays on the issue and the next startup sweep releases it; that is
        # what recovery is for, and why stopping needs to be no more graceful
        # than this.
        print("\nwatch: stopped")
        return EXIT_OK


def _saved(save: Save, config: SchedulerConfig) -> list[str]:
    """Re-validate and re-lint what was just saved, and say what it means.

    The whole of the borrowed reload loop is here: the graph is local and the
    planners are pure, so a save can be checked in milliseconds and there is
    no reason to make a developer who has just fixed a dependency wait out the
    rest of an interval to find out. Nothing is written — the pass that
    follows re-reads GitHub and re-plans, and `apply` remains the explicit act.

    A file caught mid-thought is the normal state of one being edited, so a
    graph that does not parse is reported and the loop keeps running. Exiting
    on a typo would make the scheduler the most fragile thing on the desk.
    """
    lines = [f"saved {save.describe()}"]
    for name in (*save.added, *save.edited):
        graph = _load(config.graph_path(name))
        if isinstance(graph, int):
            lines.append(f"NOTE {name} does not validate; nothing is planned from it until it does")
            continue
        specs = _read_specs(graph, config.graph_path(name))
        warnings = lint_graph(graph, specs=specs if not isinstance(specs, int) else None)
        lines.append(f"     {name}: {len(graph.tasks)} task(s), {len(warnings)} lint(s)")
        lines += [f"WARN {warning}" for warning in warnings]
    for name in save.removed:
        # `apply` already refuses to close an issue whose task has gone,
        # reporting `orphan-issue`. Silence here would let a developer believe
        # the deletion had taken effect on GitHub.
        lines.append(f"NOTE {name} removed; issues already open are not closed by a deletion")
    return lines


@contextmanager
def _dispatcher(
    args: argparse.Namespace, config: SchedulerConfig, api: GhCli | None
) -> Iterator[LocalDispatcher | None]:
    """The local lane, if it was asked for, holding the machine while it runs.

    The lockfile is taken for the whole loop rather than per pass: two `watch`
    processes would both mark and both run, and the window between passes is
    exactly when the second one would slip in.
    """
    if not getattr(args, "local", False) or api is None:
        yield None
        return
    root = work_root(Path.home())
    with hold_dispatcher(root.parent / "dispatcher.pid"):
        local = LocalDispatcher(
            api=api,
            machine=CliWorkstation(root=root, repo=Path.cwd()),
            config=config,
            root=root,
        )
        yield local


def _pass(
    args: argparse.Namespace,
    config: SchedulerConfig,
    api: GhCli | None,
    *,
    local: LocalDispatcher | None = None,
    since: TickPlan | None = None,
    number: int | None = None,
) -> tuple[int, TickPlan | None]:
    """One scheduler pass, from a fresh read.

    Every decision is recomputed here. The process may hold a *previous plan*
    for rendering — `since`, which is how the loop prints only what moved — but
    nothing survives into a decision, which is what keeps the convergence
    standard true across a restart as well as across a re-run.

    `number` is `None` for a one-shot pass, which gets no rule and no heartbeat:
    with nothing on screen above it there is nothing to separate it from, and
    `watch --once | tee` has to stay plain.
    """
    if api is not None:
        try:
            state = api.fetch_state()
        except RuntimeError as exc:
            print(f"dispatchkit: cannot read {args.repo}: {exc}", file=sys.stderr)
            return EXIT_UNREADABLE, None
    else:
        loaded = _load_state(args.state)
        if isinstance(loaded, int):
            return loaded, None
        state = loaded

    now = datetime.now(UTC)
    plan = plan_tick(
        state,
        config=config,
        now=now,
        served=served_lanes(local),
        dirty=CliWorkstation(root=Path()).dirty(plans=config.plans),
    )
    report = list(summarise_tick(plan, since=since))

    # Execution comes before the printing decision, and never depends on it.
    # The first version of this returned early on an unchanged pass, so a merge
    # GitHub had refused would never be retried while nothing else moved: the
    # engine could tell whether anyone was looking.
    if api is None:
        report.append(f"dry run: {len(plan.operations)} operation(s); re-run with --push to apply")
        return _emit(report, number), plan

    try:
        result = execute_tick(plan, api)
    except RuntimeError as exc:
        # The adapter's contract, held up (D6.6). Everything `gh` does to an
        # unattended process arrives here — a rate limit, a 502, an expiring
        # token, and the unknown label that found this — and the plan is
        # re-derived from the issues every pass, so the operations already sent
        # are all a half-finished pass leaves behind.
        _emit(report, number)
        sys.stdout.flush()
        print(f"dispatchkit: pass failed: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE, plan
    report += _local(
        local, plan, now=now, first=since is None, finish=getattr(args, "once", False)
    )
    if report or _acted(result):
        report.append(_completion(result))
        report += [f"NOTE {notice}" for notice in result.refused]
    return _emit(report, number), plan


def _local(
    local: LocalDispatcher | None,
    plan: TickPlan,
    *,
    now: datetime,
    first: bool,
    finish: bool = False,
) -> list[str]:
    """Give the dispatcher its turn, and say what it did.

    The dispatcher reads the same items the pass just resolved rather than
    re-fetching: they were read a moment ago, and a second read would be a
    second answer to a question that already has one.

    `finish` is `--once`, and it is what makes that mode work at all (D6.6).
    Returning while the run is in flight is right in the loop and only in the
    loop: there, the next pass is what a long task must not hold up. With no
    next pass the process exits, the daemon thread dies with it, and the task
    is left claimed, unstarted and one attempt poorer — while the report says
    it was dispatched.
    """
    if local is None:
        return []
    lines: list[str] = []
    if first:
        lines += [f"recover {note}" for note in local.recover(plan.items)]
    if local.busy:
        return [*lines, f"local: still running {local.running}"]
    finished = local.take_finished()
    if finished is not None:
        lines.append(_finished(finished))
    started = local.serve(plan.items, now=now, marked=plan.marked_local)
    if started is None:
        return lines
    if not finish:
        lines.append(f"local: started {started.ref}")
        return lines

    lines.append(f"local: running {started.ref}")
    local.wait()
    ran = local.take_finished()
    lines.append(_finished(ran) if ran is not None else f"local: {started.ref} did not report")
    return lines


def _finished(run: LocalRun) -> str:
    if run.ok:
        return f"local: {run.ref} finished, opened #{run.pr}"
    return f"local: {run.ref} failed at {run.stage}, branch {run.branch} kept"


def _emit(report: Sequence[str], number: int | None) -> int:
    """Print a pass. `number` is `None` for the one-shot forms, which get no
    rule and no heartbeat: there is nothing on screen above them to separate
    them from, and `watch --once | tee` has to stay plain."""
    if number is None or report:
        if number is not None:
            print(_rule(number))
        for line in report:
            print(line)
        return EXIT_OK
    # Nothing seen, nothing done. One line, with the time on it: a heartbeat
    # without a clock cannot tell "nothing has changed" from "this died an
    # hour ago".
    print(f"{_clock()} · no change")
    return EXIT_OK


def _acted(result: TickResult) -> bool:
    return bool(
        result.dispatched
        or result.readied
        or result.merged
        or result.reclaimed
        or result.closed
        or result.proposed
        or result.refused
    )


def _completion(result: TickResult) -> str:
    line = f"pass complete: {result.dispatched} dispatched"
    if result.readied:
        line += f", {result.readied} PR(s) marked ready"
    if result.merged:
        line += f", {result.merged} PR(s) merged"
    if result.reclaimed:
        line += f", {result.reclaimed} stalled dispatch(es) reclaimed"
    if result.closed:
        line += f", {result.closed} issue(s) closed"
    if result.proposed:
        line += f", {result.proposed} plan(s) proposed for review"
    return line


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _rule(number: int) -> str:
    """The separator between passes.

    Width comes from the terminal so it reaches the edge and no further; a rule
    that wraps is two rules. `get_terminal_size` answers 80 when there is no
    terminal at all, which is the right answer for a log file too.
    """
    head = f"── {_clock()}  pass {number} "
    return head + "─" * max(0, shutil.get_terminal_size().columns - len(head))


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

    plans = root / config.plans
    return LocalFacts(
        config_path=config_path,
        config_exists=config_path.exists(),
        plans=plans,
        plans_exists=plans.is_dir(),
        config=config,
        config_in_repo=_in_repo(config_path, root),
    )


def _in_repo(config_path: Path, root: Path) -> Path:
    """The config path as the repository sees it, which is what a fence covers.

    `--root ../sandbox` makes `config_path` a path from here to there; a fence
    pattern is matched against a path from the repository root, and comparing
    the two would report a missing pattern that is present.
    """
    try:
        return config_path.relative_to(root)
    except ValueError:
        return config_path


def _doctor(args: argparse.Namespace) -> int:
    facts = _facts(args)
    if isinstance(facts, int):
        return facts

    checks = check_local(facts)
    if getattr(args, "local", False):
        # Only on request. An adopter who never uses the lane has no runner
        # and is not unhealthy for it, and a check that is red for everybody
        # is a check nobody reads.
        config = _load_config(facts.config_path)
        program = config.runner.argv[0] if not isinstance(config, int) else ""
        checks = (check_runner(program, found=shutil.which(program) if program else None),) + checks
    if args.repo and shutil.which(GH) is None:
        # Nothing remote can be answered without it, and answering anyway
        # would be an invention. The local checks still run: somebody with no
        # `gh` still deserves to be told their fence is open.
        checks = (check_cli(GH, found=None),) + checks
        for line in summarise(checks):
            print(line)
        return EXIT_UNREADABLE
    if args.repo:
        api = GhCli(repo=args.repo)
        try:
            diagnostics = Diagnostics(
                scopes=api.token_scopes(),
                agent_available=api.agent_available(),
                labels=api.fetch_labels(),
                unprotected_bases=tuple(
                    base for base in _merge_targets(facts.plans) if not api.branch_protected(base)
                ),
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

    # The facts above describe the tree `init` found; these describe the tree
    # it leaves behind. Re-reading is the whole point — checked against the
    # stale facts, the directory just created still reads as missing, and the
    # config just written is never opened at all.
    after = _facts(args)
    if isinstance(after, int):
        return after
    checks = check_local(after)
    for check_line in summarise(checks):
        print(check_line)
    if healthy(checks):
        return EXIT_OK
    sys.stdout.flush()  # or the verdict arrives above the checks it is about
    print(
        "init: the repository is set up as far as `init` can take it, but a check "
        "above is failing; `init` does not overwrite a file that already exists, so "
        "the fix is yours",
        file=sys.stderr,
    )
    return EXIT_INVALID


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
