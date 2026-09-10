"""Synthetic `TaskItem` builder for the D4 resolver tests.

The resolver's input is the issue snapshot, not the graph file, so these are
built the way the scheduler sees them: a machine block plus issue state.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dispatchkit.block import MachineBlock
from dispatchkit.github import IssueState, RepoState
from dispatchkit.model import (
    DEFAULT_BASE,
    Base,
    Checks,
    Lane,
    MergedPr,
    PullRequest,
    TaskId,
    TaskRef,
    Verify,
)
from dispatchkit.resolve import TaskItem

PLAN = "demo"

#: Old enough that any stall timeout has passed. `attempts=N` synthesises N of
#: these, so a test that only cares about the count says so and nothing else.
_LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


def item(
    name: str,
    *,
    plan: str = PLAN,
    number: int | None = None,
    depends: tuple[str, ...] = (),
    lane: Lane = Lane.CLOUD,
    verify: Verify = Verify.HUMAN,
    spend: bool = False,
    touches: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    closed: bool = False,
    cancelled: bool = False,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = ("dispatchkit",),
    open_prs: tuple[int | PullRequest, ...] = (),
    attempts: int = 0,
    dispatches: tuple[datetime, ...] | None = None,
    holds: tuple[datetime, ...] = (),
) -> TaskItem:
    return TaskItem(
        block=MachineBlock(
            id=TaskId(name),
            plan=plan,
            milestone="M",
            lane=lane,
            requires=requires,
            verify=verify,
            spend=spend,
            depends=tuple(TaskId(dep) for dep in depends),
            touches=touches,
        ),
        number=number if number is not None else abs(hash(name)) % 900 + 1,
        closed=closed or cancelled,
        cancelled=cancelled,
        assignees=assignees,
        labels=labels,
        open_prs=_prs(open_prs),
        dispatches=(dispatches if dispatches is not None else (_LONG_AGO,) * attempts),
        holds=holds,
    )


def items_of(*entries: TaskItem) -> tuple[TaskItem, ...]:
    return entries


def ref(name: str, plan: str = PLAN) -> TaskRef:
    """How a task is keyed once every plan is in the room (D13)."""
    return TaskRef(plan, TaskId(name))


def issue(
    name: str,
    number: int,
    *,
    plan: str = PLAN,
    depends: tuple[str, ...] = (),
    lane: Lane = Lane.CLOUD,
    verify: Verify = Verify.HUMAN,
    spend: bool = False,
    touches: tuple[str, ...] = (),
    closed: bool = False,
    cancelled: bool = False,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = ("dispatchkit",),
    open_prs: tuple[int | PullRequest, ...] = (),
    dispatches: tuple[datetime, ...] = (),
    holds: tuple[datetime, ...] = (),
    acceptance: str = "check && verify",
    base: Base = DEFAULT_BASE,
    merged: tuple[MergedPr, ...] = (),
    closed_at: datetime | None = None,
    node_id: str | None = None,
) -> IssueState:
    """The same synthetic task, but as GitHub would hand it back."""
    block = MachineBlock(
        id=TaskId(name),
        plan=plan,
        milestone="M",
        lane=lane,
        requires=("gpu",) if lane is Lane.LOCAL else (),
        verify=verify,
        spend=spend,
        depends=tuple(TaskId(dep) for dep in depends),
        touches=touches,
        base=base,
    )
    return IssueState(
        number=number,
        title=name,
        body=(
            f"{name} prose\n\n## Acceptance\n\n```sh\n{acceptance}\n```\n\n"
            f"{render_raw_block(block)}\n"
        ),
        labels=labels,
        closed=closed or cancelled,
        cancelled=cancelled,
        assignees=assignees,
        open_prs=_prs(open_prs),
        dispatches=dispatches,
        holds=holds,
        merged=merged,
        closed_at=closed_at,
        node_id=node_id if node_id is not None else f"I_{number}",
    )


def render_raw_block(block: MachineBlock) -> str:
    """Render a `MachineBlock` back into body text, for building fixtures."""
    values = {
        "v": str(block.version),
        "id": block.id,
        "plan": block.plan,
        "milestone": block.milestone,
        "lane": block.lane.value,
        "requires": "[" + ", ".join(block.requires) + "]",
        "verify": block.verify.value,
        "spend": "true" if block.spend else "false",
        "depends": "[" + ", ".join(block.depends) + "]",
        "touches": "[" + ", ".join(f'"{pattern}"' for pattern in block.touches) + "]",
    }
    # Only from the version that introduced it, so a v1 fixture stays a v1
    # fixture and keeps proving that an older block still reads (D16).
    if block.version >= 2:
        values["base"] = str(block.base)
    body = "\n".join(f"{key}: {value}" for key, value in values.items())
    return f"<!-- dispatchkit\n{body}\n-->"


def state_of(*issues: IssueState) -> RepoState:
    return RepoState(issues)


def _prs(entries: tuple[int | PullRequest, ...]) -> tuple[PullRequest, ...]:
    """Accept a bare PR number where the checks do not matter to the test.

    Most tests care only that *a* pull request exists. Spelling out
    `PullRequest(7, Checks.NONE)` everywhere would bury the few tests where the
    check state is the entire point.

    The shorthand is mergeable, which is the ordinary case at GitHub — the
    field defaults to false on `PullRequest` itself, where the safe reading of
    an unanswered question has to win.
    """
    return tuple(
        entry if isinstance(entry, PullRequest) else PullRequest(entry, Checks.NONE, mergeable=True)
        for entry in entries
    )
