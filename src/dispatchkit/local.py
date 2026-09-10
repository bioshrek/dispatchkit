"""D6.3: running one task on this machine.

The executor owns everything except the change itself: it fetches, branches,
makes a worktree, builds the prompt, invokes the agent, runs `acceptance`,
pushes and opens the pull request. The agent is handed a prepared, disposable
tree and asked to do exactly one thing.

Sequenced here, effected through two ports — `GitHubApi` for the repository and
`Workstation` for the machine — so the whole of it is testable with no
subprocess, no git and no network.

Three orderings carry the design rather than merely implementing it.

**`acceptance` before the pull request.** A pull request is a claim that the
work is done, and for `verify: auto` the next pass merges it. Opening one and
letting CI find out would be true only when CI happens to run the same command,
which is precisely the assumption `acceptance-not-in-ci` exists to refuse.

**The push in the parent.** The child runs with no credential and no agent
socket, so it *cannot* push; the executor does it. That separation falls out of
the environment allowlist rather than being a rule somebody has to remember.

**`Closes #N` written here, never prompted for.** An agent that forgot it would
merge a pull request while leaving the issue open, stalling every dependent
with no error anywhere.

And one rule about failure: a failed run keeps its worktree and pushes whatever
it committed. That branch is **evidence, not state** — a human may read it, and
nothing in the system ever does. Resuming a dead agent's run is not reliably
possible, so the retry starts clean, which is also what keeps the retry budget
honest.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dispatchkit.config import SchedulerConfig
from dispatchkit.errors import GraphError
from dispatchkit.github import LABEL_LOCAL_CLAIM, GitHubApi
from dispatchkit.resolve import TaskItem
from dispatchkit.workstation import (
    RunResult,
    Workstation,
    acceptance_argv,
    acceptance_of,
    branch_name,
    prompt_for,
    worktree_path,
)

#: How much of a run's output is posted back to the issue. The full trace stays
#: in the local log; the tail is what makes a failure legible to somebody
#: reading GitHub, which is where every other lane reports.
TAIL = 4000


@dataclass(frozen=True, slots=True)
class LocalRun:
    """What happened, in the terms the report and the issue both need."""

    ref: str
    branch: str
    stage: str
    ok: bool
    detail: str = ""
    pr: int | None = None


def run_local(
    task: TaskItem,
    *,
    api: GitHubApi,
    machine: Workstation,
    config: SchedulerConfig,
    root: Path,
    now: datetime,
    environ: Mapping[str, str] | None = None,
) -> LocalRun:
    """Take one marked local task all the way, or stop at the first failure."""
    runner = config.runner
    ref = task.ref
    attempt = task.attempts + 1
    branch = branch_name(ref, attempt)
    path = worktree_path(root, ref)
    env = runner.environment(os.environ if environ is None else environ)

    try:
        argv = runner.render(
            prompt=prompt_for(task.title, task.body),
            worktree=str(path),
            model=runner.model,
            effort=None,
        )
        clauses = acceptance_argv(acceptance_of(task.body))
    except GraphError as exc:
        # A template needing a value this task does not set, or a body with no
        # acceptance in it. Neither is recoverable by trying harder, and
        # neither should reach the machine.
        return _finish(
            api, task, LocalRun(str(ref), branch, "prepare", False, exc.issues[0].message)
        )

    if not clauses:
        return _finish(
            api,
            task,
            LocalRun(
                str(ref),
                branch,
                "prepare",
                False,
                "the issue body carries no `## Acceptance` command, so nothing could "
                "decide whether this task was done",
            ),
        )

    made = machine.create_worktree(path=path, branch=branch)
    if not made.ok:
        # Nothing to preserve and nothing to remove: there is no tree.
        return _finish(api, task, _failed(ref, branch, "worktree", made))

    agent = machine.run(argv, cwd=path, env=env, timeout=runner.timeout)
    if not agent.ok:
        # Acceptance is not attempted: there is nothing to accept, and running
        # it would report a second failure for the same cause.
        return _finish(
            api, task, _failed(ref, branch, "agent", agent), machine=machine, path=path
        )

    for clause in clauses:
        checked = machine.run(list(clause), cwd=path, env=env, timeout=runner.timeout)
        if not checked.ok:
            return _finish(
                api,
                task,
                _failed(ref, branch, "acceptance", checked),
                machine=machine,
                path=path,
            )

    pushed = machine.push(path=path, branch=branch)
    if not pushed.ok:
        return _finish(
            api, task, _failed(ref, branch, "push", pushed), machine=machine, path=path
        )

    number = api.open_pr(
        head=branch,
        title=task.title,
        body=f"{prompt_for(task.title, task.body)}\n\nCloses #{task.number}\n",
    )
    machine.remove_worktree(path=path)
    return _finish(
        api,
        task,
        LocalRun(str(ref), branch, "done", True, agent.output, pr=number),
    )


def _failed(ref: object, branch: str, stage: str, result: RunResult) -> LocalRun:
    detail = "timed out" if result.timed_out else result.output
    return LocalRun(str(ref), branch, stage, False, detail)


def _finish(
    api: GitHubApi,
    task: TaskItem,
    run: LocalRun,
    *,
    machine: Workstation | None = None,
    path: Path | None = None,
) -> LocalRun:
    """Report, preserve, release — in that order, and always.

    The mark comes off whatever happened, including when a human held the task
    mid-run. Leaving it on is the D6.0 bug from the other end: the task would
    read `Dispatched` for ever with nothing running it. Releasing is also the
    retry, since the next pass re-marks it and the mark is the event `attempts`
    derives from — and a hold needs no help here, because it already gates
    `ready_ops`, so releasing a held task returns it to the queue without
    returning it to the ready set.
    """
    if machine is not None and path is not None and not run.ok and machine.commits(path=path) > 0:
        machine.push(path=path, branch=run.branch)

    api.comment(number=task.number, body=_report(run))
    api.edit_labels(number=task.number, remove=(LABEL_LOCAL_CLAIM,))
    return run


def _report(run: LocalRun) -> str:
    if run.ok:
        return f"`dispatchkit` ran this locally and opened #{run.pr}.\n\n_Branch `{run.branch}`._"
    tail = run.detail[-TAIL:] if run.detail else "(no output)"
    return (
        f"`dispatchkit` ran this locally and stopped at **{run.stage}**.\n\n"
        f"```\n{tail}\n```\n\n"
        f"_Branch `{run.branch}`, kept for inspection. The task is back in the queue; "
        "nothing here is read back._"
    )
