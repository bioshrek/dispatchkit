"""D4: the readiness resolver — the scheduler's brain, as a pure function.

Two claims are being made here, and both are the reason this module contains no
I/O at all:

1. **Status is derived, never stored.** `Status` is recomputed from the issues
   on every pass and printed; it is written nowhere, so there is nothing that
   could be stale and nothing to reconcile. There is no state machine to get
   wedged, because there is no state.
2. **Assignment is the lock.** A task is ready only while it is unassigned, so
   dispatching it (assigning it) removes it from the ready set. Two schedulers
   racing on the same repo therefore converge instead of double-dispatching,
   without a lease, a lockfile or a mutex — GitHub's own write is the lock.

Readiness is deliberately a *one-step* check ("are my direct dependencies
closed?") rather than a recursive walk. That makes a cycle harmless: nothing in
it ever opens, but the resolver still terminates.

Admission is kept separate from status. A task waiting on spend approval or
sitting at its retry budget is still `Ready` — the work is unblocked, we are
just choosing not to start it. Folding those into `Status` would invent
statuses the plan does not define and would lose the distinction between
"cannot run" and "will not run yet".
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from fnmatch import fnmatch

from dispatchkit.block import MachineBlock, parse_block
from dispatchkit.config import SchedulerConfig
from dispatchkit.errors import GraphError
from dispatchkit.github import (
    DISPATCHKIT_LABEL,
    LABEL_HOLD,
    LabelIssue,
    MarkReady,
    MergePr,
    Notice,
    RepoState,
    UnassignAgent,
)
from dispatchkit.model import Checks, Lane, PullRequest, TaskId, TaskRef, Verify

#: The cloud agent's own login. GitHub records a second AssignedEvent for the
#: human who triggered the dispatch, so both the count of attempts and the
#: reclaim have to name the agent rather than take the assignee list wholesale
#: -- otherwise one dispatch reads as two, and a reclaim unassigns a watching
#: human. Confirmed live: issue #3 carried `copilot-swe-agent` and `bioshrek`.
AGENT_LOGINS = frozenset({"copilot-swe-agent", "Copilot"})

LABEL_STUCK = "dispatch:stuck"
LABEL_SPEND_APPROVED = "spend:approved"
LABEL_LOCAL_CLAIM = "dispatch:local"

_WILDCARD = "*?["


class Status(Enum):
    """What the issues say a task is doing. Derived every pass, stored nowhere.

    Eight of the nine are a restatement of something GitHub already shows —
    dependencies open or closed, an assignee, an open pull request, two
    labels, a closed issue and how it was closed. `Auto-merging` is the only synthesized
    one, and it is the reason the report is worth printing at all.
    """

    BLOCKED = "Blocked"
    CANCELLED = "Cancelled"
    HELD = "Held"
    READY = "Ready"
    DISPATCHED = "Dispatched"
    IN_REVIEW = "In Review"
    AUTO_MERGING = "Auto-merging"
    STUCK = "Stuck"
    DONE = "Done"


#: Statuses that occupy a lane slot: work an agent is already doing.
IN_FLIGHT = frozenset({Status.DISPATCHED, Status.IN_REVIEW, Status.AUTO_MERGING})


@dataclass(frozen=True, slots=True)
class TaskItem:
    """One task as the scheduler sees it: machine block plus live issue state."""

    block: MachineBlock
    number: int
    closed: bool
    assignees: tuple[str, ...]
    labels: tuple[str, ...]
    open_prs: tuple[PullRequest, ...]
    dispatches: tuple[datetime, ...]
    holds: tuple[datetime, ...] = ()
    node_id: str | None = None
    #: Closed as not planned. Closed, but satisfying nothing (D13.1).
    cancelled: bool = False

    @property
    def id(self) -> TaskId:
        return self.block.id

    @property
    def ref(self) -> TaskRef:
        """How this task is named outside its own plan (D13)."""
        return TaskRef(self.block.plan, self.block.id)

    @property
    def lane(self) -> Lane:
        return self.block.lane

    @property
    def verify(self) -> Verify:
        return self.block.verify

    @property
    def touches(self) -> tuple[str, ...]:
        return self.block.touches

    @property
    def attempts(self) -> int:
        """How many times this task has been handed to an agent.

        Counted from the issue's own assignment history, which GitHub keeps
        whatever happens to the assignment: a number only we remembered would
        be the one piece of scheduler state GitHub could not rebuild.

        A dispatch a human held is not counted (D13.1c). The budget exists to
        stop a task that keeps failing, and an interruption is not a failure —
        charging it would let three holds exhaust a healthy task's budget and
        mark it `Stuck`, the scheduler punishing someone for using the control
        it gave them. A hold is credited against the dispatch it interrupted:
        the one it followed, before the next began.
        """
        return sum(1 for start, end in self._runs() if not self._held_between(start, end))

    def _runs(self) -> list[tuple[datetime, datetime | None]]:
        """Each dispatch, paired with the moment the next one superseded it."""
        starts = sorted(self.dispatches)
        return [
            (start, starts[position + 1] if position + 1 < len(starts) else None)
            for position, start in enumerate(starts)
        ]

    def _held_between(self, start: datetime, end: datetime | None) -> bool:
        return any(start <= hold and (end is None or hold < end) for hold in self.holds)

    @property
    def stuck(self) -> bool:
        return LABEL_STUCK in self.labels

    @property
    def held(self) -> bool:
        """Has a human said "not now"? (D13.1c)

        A standing decision, not a scheduling one — which is why it reads as a
        status rather than a deferral, and why it stops merging as well as
        dispatch. Merging is the only thing dispatchkit does that changes
        `main` without a human, so a hold that let it through would fail at the
        moment the control matters most.
        """
        return LABEL_HOLD in self.labels

    @property
    def claimed(self) -> bool:
        """Has an agent taken this task? Cloud assigns; the local daemon labels."""
        return bool(self.assignees) or LABEL_LOCAL_CLAIM in self.labels

    @property
    def checks(self) -> Checks:
        """CI's verdict across every open PR on this task, worst first."""
        return Checks.combine(pr.checks for pr in self.open_prs)

    @property
    def mergeable(self) -> bool:
        """Can every open PR actually be merged into the base?

        `all`, and false when there are no PRs at all, because this only ever
        gates a claim that a merge is coming.
        """
        return bool(self.open_prs) and all(pr.mergeable for pr in self.open_prs)

    @property
    def draft(self) -> bool:
        """Is any open PR still a draft, and so unmergeable?

        `any`, not `all`: one draft is enough to stop the task closing, and
        the pessimistic reading is the safe one here as everywhere else.
        """
        return any(pr.draft for pr in self.open_prs)


@dataclass(frozen=True, slots=True)
class Deferral:
    ref: TaskRef
    reason: str
    detail: str = ""

    def __str__(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.ref}: {self.reason}{suffix}"


@dataclass(frozen=True, slots=True)
class AdmissionPlan:
    admitted: tuple[TaskRef, ...]
    deferred: tuple[Deferral, ...]


def build_items(
    state: RepoState, *, plan: str | None = None
) -> tuple[tuple[TaskItem, ...], tuple[Notice, ...]]:
    """Turn a repo snapshot into resolver input, in issue order.

    `plan` narrows the result to one graph file, which is what a single-plan
    report wants. A pass passes nothing: `watch` schedules every plan in the
    repository at once, because the caps bound agents and a machine, neither
    of which knows what a plan is (D13).
    """
    items: list[TaskItem] = []
    notices: list[Notice] = []

    for issue in sorted(state.issues, key=lambda issue: issue.number):
        if DISPATCHKIT_LABEL not in issue.labels:
            continue
        try:
            block = parse_block(issue.body)
        except GraphError as exc:
            notices.append(
                Notice(
                    "unreadable-issue",
                    f"#{issue.number}",
                    f"dispatchkit block is unreadable ({exc.issues[0].code}); "
                    "the scheduler will ignore this issue",
                )
            )
            continue
        if plan is not None and block.plan != plan:
            continue
        items.append(
            TaskItem(
                block=block,
                number=issue.number,
                closed=issue.closed,
                cancelled=issue.cancelled,
                assignees=issue.assignees,
                labels=issue.labels,
                open_prs=issue.open_prs,
                dispatches=issue.dispatches,
                holds=issue.holds,
                node_id=issue.node_id,
            )
        )
    return tuple(items), tuple(notices)


def _satisfied(items: Sequence[TaskItem]) -> set[TaskRef]:
    """The tasks a dependency edge may be discharged against.

    Closed *and* completed. A task closed as not planned is finished with but
    was never done, so releasing its dependents would dispatch work whose
    prerequisite does not exist — which was live until D13.1, and reachable by
    anyone tidying a backlog from the web UI.
    """
    return {task.ref for task in items if task.closed and not task.cancelled}


def resolve(items: Sequence[TaskItem]) -> dict[TaskRef, Status]:
    """Derive every task's status from the issues alone.

    Keyed by `TaskRef`, not by `TaskId`: `watch` pools every plan in the
    repository, and two plans may both contain `ports` (D13).
    """
    done = _satisfied(items)
    return {task.ref: _status_of(task, done) for task in items}


def blocking(items: Sequence[TaskItem]) -> dict[TaskRef, tuple[TaskId, ...]]:
    """For each task, the dependencies that are not closed yet.

    The report's answer to the only question a status list cannot answer on its
    own: `Blocked` says a task is not moving, not what would move it. It is a
    one-hop question, so this returns one hop — the closure is what a graph
    would draw, and drawing it is not worth a layout algorithm in a terminal.

    Empty for anything ready or already running, so a caller can annotate
    unconditionally and get nothing where there is nothing to say.
    """
    done = _satisfied(items)
    return {
        task.ref: tuple(dep for dep in task.block.depends if task.ref.sibling(dep) not in done)
        for task in items
    }


def _status_of(task: TaskItem, closed: frozenset[TaskRef] | set[TaskRef]) -> Status:
    if task.cancelled:
        # Read before `closed`: a cancelled issue is closed too, and calling it
        # `Done` is the whole bug.
        return Status.CANCELLED
    if task.closed:
        return Status.DONE
    if task.held:
        # Read before everything an open issue could otherwise say, including
        # `Stuck`: both may be true, but only one of them is a decision
        # somebody made, and blaming the retry budget for a human's choice
        # sends the reader to the wrong repair.
        return Status.HELD
    if task.stuck:
        # Read before `Ready`, because a stuck task is unassigned and would
        # otherwise satisfy every readiness test while never being dispatched
        # again -- the report claiming work is queued that never moves.
        return Status.STUCK
    if task.open_prs:
        # A PR exists, so the work happened regardless of how it was claimed;
        # `verify` decides who closes it out. `Auto-merging` is a claim that
        # CI is going to decide, so it is only made while CI is in a position
        # to: a run held for approval, or a red one, needs a human, which is
        # exactly what `In Review` means. Saying `Auto-merging` over a pipeline
        # that will never start would be the report asserting something false.
        # A draft is the same lie by a different route — green CI merges
        # nothing while the PR cannot be merged at all.
        # A conflicting pull request is the same lie by a third route: green,
        # out of draft, and unmergeable until a human rebases it. Only a human
        # can move it, so `In Review` is the honest word.
        if (
            task.verify is Verify.AUTO
            and not task.checks.stalled
            and not task.draft
            and task.mergeable
        ):
            return Status.AUTO_MERGING
        return Status.IN_REVIEW
    if task.claimed:
        return Status.DISPATCHED
    # A dependency with no issue at all (deleted, or never applied) is not
    # closed, so its dependents stay blocked. Failing safe beats guessing.
    if all(task.ref.sibling(dep) in closed for dep in task.block.depends):
        return Status.READY
    return Status.BLOCKED


def ready_ops(items: Sequence[TaskItem]) -> tuple[MarkReady, ...]:
    """Take `verify: auto` pull requests out of draft once CI has passed.

    Copilot finishes its work and leaves the pull request a draft — that is
    deliberate on GitHub's part, and for `verify: human` it is exactly right,
    because marking it ready *is* the reviewer's act. For `verify: auto` there
    is no reviewer by definition, so leaving it would mean a task that can
    never close: `auto` says the pipeline decides, and the pipeline has.

    Guarded on `PASSING` rather than on "not stalled". `Checks.NONE` is an
    absence of evidence, and clearing a merge gate on the strength of no runs
    at all would be a worse version of the bug this whole deliverable exists
    to fix.
    """
    operations = []
    for task in items:
        if task.closed or task.held or task.verify is not Verify.AUTO:
            continue
        operations += [
            MarkReady(task.ref, pr.number)
            for pr in task.open_prs
            if pr.draft and pr.checks is Checks.PASSING
        ]
    return tuple(operations)


def merge_ops(items: Sequence[TaskItem], config: SchedulerConfig) -> tuple[MergePr, ...]:
    """Merge the `verify: auto` pull requests that have earned it.

    This is the only thing dispatchkit does that changes `main` without a human,
    so every gate is checked here rather than delegated. The probe that produced
    this function found the repository is not a backstop: on a branch with no
    protection GitHub merged a pull request instantly and reported success,
    having consulted no check at all. Branch protection is a *second* lock when
    it exists, never the first one.

    Four conditions, each of which has already been the subject of a bug:

    - `verify: auto`, because `human` means a person merges.
    - Not a draft, because a draft merges nothing (D5.7).
    - Actually mergeable. Green is a statement about CI, not about whether the
      branch still applies; a passing pull request that conflicts with the base
      merges nowhere, and asking anyway is a hard error.
    - `Checks.PASSING` exactly, not merely "not stalled" (D5.6). `NONE` is an
      absence of evidence and is the state an unconfigured repository sits in
      forever.
    - Nothing inside the blast-radius fence, so the pipeline cannot rewrite its
      own workflow, config or task graph unattended.
    - No scope drift: every file is one the task declared it would touch. An
      agent outside its blast radius is the case the design says not to merge
      unattended, and it is not hypothetical — a drifting pull request caused a
      real collision here, because the concurrency exclusion reasons about
      *declared* scope and had nothing to go on.
    """
    operations = []
    for task in items:
        if task.closed or task.held or task.verify is not Verify.AUTO:
            continue
        operations += [
            MergePr(task.ref, pr.number)
            for pr in task.open_prs
            if not pr.draft
            and pr.mergeable
            and pr.checks is Checks.PASSING
            and pr.files
            and not any(config.is_fenced(path) for path in pr.files)
            and _within_scope(pr.files, task.touches)
        ]
    return tuple(operations)


def stall_ops(
    items: Sequence[TaskItem],
    config: SchedulerConfig,
    now: datetime,
) -> tuple[UnassignAgent | LabelIssue, ...]:
    """Reclaim dispatches that produced nothing, and stop reclaiming forever.

    The timeout asks one question: did this dispatch produce a pull request?
    Not whether it was merged, and not whether review finished -- a long review
    is not a stall, and reclaiming one would throw away real work.

    Releasing the assignment *is* the retry. Assignment is the dispatch lock,
    so an unassigned task rejoins the ready set on the next pass with nothing
    else written down, and the attempt that just failed is still counted
    because the timeline keeps it.
    """
    ops: list[UnassignAgent | LabelIssue] = []
    for task in items:
        if not _stalled(task, config, now):
            continue
        agent = tuple(name for name in task.assignees if name in AGENT_LOGINS)
        ops.append(UnassignAgent(task.ref, task.number, agent or task.assignees))
        if task.attempts >= config.retry_budget:
            # Handing it back now would spend a fourth attempt on a budget of
            # three, so the reclaim is the last thing that happens to it.
            ops.append(LabelIssue(task.ref, task.number, add=(LABEL_STUCK,)))
    return tuple(ops)


def _stalled(task: TaskItem, config: SchedulerConfig, now: datetime) -> bool:
    if task.closed or task.held or task.stuck or task.open_prs or not task.assignees:
        return False
    if not task.dispatches:
        return False
    return now - max(task.dispatches) >= config.stall_after


def _within_scope(files: Sequence[str], touches: Sequence[str]) -> bool:
    """Did this pull request stay inside the scope its task declared?

    An empty `touches` means unknown scope. For the concurrency exclusion that
    reads as "conflicts with nothing", which is the right permissive answer to
    a scheduling question. Here it is an unanswerable question about whether an
    agent stayed where it said it would, and the answer to those is no.
    """
    if not touches:
        return False
    return all(any(fnmatch(path, pattern) for pattern in touches) for path in files)


def stranded_notices(items: Sequence[TaskItem]) -> tuple[Notice, ...]:
    """Report the tasks a cancellation has stranded for good.

    `Blocked` reads as "not yet". A task waiting on something closed as not
    planned is not waiting — it can never run, and nothing else in the report
    says so. This is the same family as the dangling-dependency error: a graph
    that silently stops is the failure this system exists to prevent.

    Reported transitively, because naming only the immediate dependent invites
    someone to unblock it and expect the rest to follow.
    """
    order = {task.ref: position for position, task in enumerate(items)}
    dependents: dict[TaskRef, list[TaskRef]] = {}
    for task in items:
        for dep in task.block.depends:
            dependents.setdefault(task.ref.sibling(dep), []).append(task.ref)

    notices = []
    for task in items:
        if not task.cancelled:
            continue
        stranded = _reachable(task.ref, dependents, closed={t.ref for t in items if t.closed})
        if not stranded:
            continue
        listed = ", ".join(str(ref) for ref in sorted(stranded, key=lambda ref: order[ref]))
        notices.append(
            Notice(
                "cancelled-dependency",
                f"#{task.number}",
                f"{task.ref} was closed as not planned, so {listed} can never run. "
                "Reopen it if the work is still needed, or close them as not "
                "planned too.",
            )
        )
    return tuple(notices)


def _reachable(
    start: TaskRef, dependents: dict[TaskRef, list[TaskRef]], *, closed: set[TaskRef]
) -> set[TaskRef]:
    """Everything downstream of `start`, stopping at anything already closed.

    A closed task is not stranded whatever its dependencies did — somebody did
    the work anyway, or decided it was not needed — and its own dependents are
    released by it rather than held by the cancellation behind it.
    """
    found: set[TaskRef] = set()
    frontier = list(dependents.get(start, ()))
    while frontier:
        ref = frontier.pop()
        if ref in found or ref in closed:
            continue
        found.add(ref)
        frontier.extend(dependents.get(ref, ()))
    return found


def ci_notices(items: Sequence[TaskItem]) -> tuple[Notice, ...]:
    """Report `verify: auto` tasks whose CI cannot run without a human.

    Absorbing this into `In Review` alone would be quietly misleading: the
    report would invite someone to review a pull request that cannot merge, and
    the reason would be a checkbox two pages deep in the Actions tab. Only
    `BLOCKED` is reported — a failing run is somebody's bug, not a gate that
    can be clicked away, and suggesting otherwise would waste the reader's
    time.
    """
    notices = []
    for task in items:
        if task.closed or task.verify is not Verify.AUTO:
            continue
        held = [pr.number for pr in task.open_prs if pr.checks is Checks.BLOCKED]
        if not held:
            continue
        listed = ", ".join(f"#{number}" for number in held)
        notices.append(
            Notice(
                "ci-approval-required",
                f"#{task.number}",
                f"{listed}: GitHub is holding the workflow runs for approval, so "
                "`verify: auto` cannot complete. Approve them in the Actions tab, "
                "or re-queue with `gh run rerun <id>` — note that either way you "
                "are choosing to run agent-authored code.",
            )
        )
    return tuple(notices)


def admit(
    items: Sequence[TaskItem],
    statuses: Mapping[TaskRef, Status],
    config: SchedulerConfig,
    *,
    served: Collection[Lane] = tuple(Lane),
) -> AdmissionPlan:
    """Choose which ready tasks to start now, respecting caps, spend and scope.

    Plan order is ascending issue number, which is the order `apply` created
    them, which is the order they appear in the graph file — so the author's
    ordering is the tie-break, and the choice is reproducible.

    `served` is which lanes the caller can actually hand work to. A lane the
    running build cannot serve has to be refused *here* rather than at the
    dispatch itself: admission is also where file scope is claimed, so a task
    admitted and then quietly not dispatched went on excluding real work from
    the other lane for as long as it sat there.
    """
    in_flight: dict[Lane, int] = {}
    active: list[tuple[TaskRef, tuple[str, ...]]] = []
    for task in items:
        if statuses[task.ref] in IN_FLIGHT:
            in_flight[task.lane] = in_flight.get(task.lane, 0) + 1
            active.append((task.ref, task.touches))

    admitted: list[TaskRef] = []
    deferred: list[Deferral] = []

    for task in sorted(items, key=lambda task: task.number):
        if statuses[task.ref] is not Status.READY:
            continue
        deferral = _gate(task, config, in_flight, active, served)
        if deferral is not None:
            deferred.append(deferral)
            continue
        admitted.append(task.ref)
        in_flight[task.lane] = in_flight.get(task.lane, 0) + 1
        active.append((task.ref, task.touches))

    return AdmissionPlan(tuple(admitted), tuple(deferred))


def _gate(
    task: TaskItem,
    config: SchedulerConfig,
    in_flight: Mapping[Lane, int],
    active: Sequence[tuple[TaskRef, tuple[str, ...]]],
    served: Collection[Lane],
) -> Deferral | None:
    if task.lane not in served:
        # Read first: every gate below it describes a queue this task is not
        # in. A lane with nothing behind it is not busy, it is absent, and
        # `lane-cap` would send the reader to raise a cap that would change
        # nothing.
        return Deferral(
            task.ref,
            "no-executor",
            f"nothing in this build runs lane:{task.lane.value}",
        )
    if LABEL_STUCK in task.labels:
        return Deferral(task.ref, "stuck", "labelled dispatch:stuck")
    if task.attempts >= config.retry_budget:
        return Deferral(
            task.ref, "stuck", f"{task.attempts} attempts, budget {config.retry_budget}"
        )
    if task.block.spend and LABEL_SPEND_APPROVED not in task.labels:
        return Deferral(task.ref, "awaiting-spend-approval", "add `spend:approved` to release")
    cap = config.cap(task.lane)
    if in_flight.get(task.lane, 0) >= cap:
        return Deferral(task.ref, "lane-cap", f"{task.lane.value} cap {cap}")
    blocker = _scope_conflict(task, active)
    if blocker is not None:
        return Deferral(task.ref, "file-scope-conflict", f"overlaps {blocker}")
    return None


def _scope_conflict(
    task: TaskItem, active: Iterable[tuple[TaskRef, tuple[str, ...]]]
) -> TaskRef | None:
    for other_ref, other_touches in active:
        if any(_overlaps(mine, theirs) for mine in task.touches for theirs in other_touches):
            return other_ref
    return None


def _overlaps(left: str, right: str) -> bool:
    """Would two `touches` patterns plausibly hit the same file?

    Compared on literal prefixes only — no globbing, no filesystem. The result
    is conservative in one direction on purpose: it may serialise two tasks
    that would in fact have been fine, but it will not let two agents edit the
    same tree at once. An unnecessary wait costs minutes; a bad merge costs a
    human's afternoon.
    """
    left_dir, left_wild = _prefix(left)
    right_dir, right_wild = _prefix(right)
    if not (left_wild or right_wild):
        return left == right
    return left_dir.startswith(right_dir) or right_dir.startswith(left_dir)


def _prefix(pattern: str) -> tuple[str, bool]:
    """The pattern's fixed directory prefix, and whether it globs at all."""
    cut = min((pattern.find(char) for char in _WILDCARD if char in pattern), default=-1)
    if cut < 0:
        head, _, _ = pattern.rpartition("/")
        return f"{head}/" if head else "", False
    head = pattern[:cut]
    head, _, _ = head.rpartition("/")
    return f"{head}/" if head else "", True
