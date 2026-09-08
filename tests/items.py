"""Synthetic `TaskItem` builder for the D4 resolver tests.

The resolver's input is the issue snapshot, not the graph file, so these are
built the way the scheduler sees them: a machine block plus issue state.
"""

from __future__ import annotations

from dispatchkit.block import MachineBlock
from dispatchkit.github import IssueState, RepoState
from dispatchkit.model import Checks, Lane, PullRequest, TaskId, Verify
from dispatchkit.resolve import TaskItem

PLAN = "demo"


def item(
    name: str,
    *,
    number: int | None = None,
    depends: tuple[str, ...] = (),
    lane: Lane = Lane.CLOUD,
    verify: Verify = Verify.HUMAN,
    spend: bool = False,
    touches: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    closed: bool = False,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = ("dispatchkit",),
    open_prs: tuple[int | PullRequest, ...] = (),
    attempts: int = 0,
    fields: dict[str, str] | None = None,
    project_item_id: str | None = "PVTI_1",
) -> TaskItem:
    return TaskItem(
        block=MachineBlock(
            id=TaskId(name),
            plan=PLAN,
            milestone="M",
            lane=lane,
            requires=requires,
            verify=verify,
            spend=spend,
            depends=tuple(TaskId(dep) for dep in depends),
            touches=touches,
        ),
        number=number if number is not None else abs(hash(name)) % 900 + 1,
        closed=closed,
        assignees=assignees,
        labels=labels,
        open_prs=_prs(open_prs),
        attempts=attempts,
        project_item_id=project_item_id,
        fields=fields if fields is not None else {},
    )


def items_of(*entries: TaskItem) -> tuple[TaskItem, ...]:
    return entries


def issue(
    name: str,
    number: int,
    *,
    depends: tuple[str, ...] = (),
    lane: Lane = Lane.CLOUD,
    verify: Verify = Verify.HUMAN,
    spend: bool = False,
    touches: tuple[str, ...] = (),
    closed: bool = False,
    assignees: tuple[str, ...] = (),
    labels: tuple[str, ...] = ("dispatchkit",),
    open_prs: tuple[int | PullRequest, ...] = (),
    fields: dict[str, str] | None = None,
    node_id: str | None = None,
) -> IssueState:
    """The same synthetic task, but as GitHub would hand it back."""
    block = MachineBlock(
        id=TaskId(name),
        plan=PLAN,
        milestone="M",
        lane=lane,
        requires=("gpu",) if lane is Lane.LOCAL else (),
        verify=verify,
        spend=spend,
        depends=tuple(TaskId(dep) for dep in depends),
        touches=touches,
    )
    return IssueState(
        number=number,
        title=name,
        body=f"prose\n\n{render_raw_block(block)}\n",
        labels=labels,
        closed=closed,
        # One board item per issue: sharing an id across fixtures would let a
        # write for one task silently land on another.
        project_item_id=f"PVTI_{number}",
        fields=fields if fields is not None else {},
        assignees=assignees,
        open_prs=_prs(open_prs),
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
        entry
        if isinstance(entry, PullRequest)
        else PullRequest(entry, Checks.NONE, mergeable=True)
        for entry in entries
    )
