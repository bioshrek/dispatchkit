"""In-memory GitHub double for the D3 replay tests.

Seeded from a `RepoState` (itself parsed from a recorded API fixture), it
applies mutations to its own store and can hand back a fresh `RepoState`. That
round trip is what makes the idempotency claim testable: apply, re-read, plan
again, and assert the second plan is empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from dispatchkit.gh_cli import AGENT_LOGIN
from dispatchkit.github import IssueState, RepoState
from dispatchkit.model import DEFAULT_BASE, Base


@dataclass
class FakeGitHub:
    state: RepoState = field(default_factory=lambda: RepoState(()))
    next_number: int = 100
    calls: list[str] = field(default_factory=list)
    ensured_labels: set[str] = field(default_factory=set)
    #: What the double stamps on an assignment. A test that cares about the
    #: stall moves it; everything else never reads it.
    clock: datetime = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    #: The seed, kept so a subclass can refuse to move. Only `TestPrinting-
    #: NeverGatesWorking` uses it, to hold a pass's input still.
    original: RepoState = field(default_factory=lambda: RepoState(()))

    #: The local lane's two writes (D6), kept for assertion rather than replay.
    opened: list[dict[str, str]] = field(default_factory=list)
    comments: list[dict[str, str]] = field(default_factory=list)
    #: Plan branches `apply` asked for (D16).
    ensured_branches: list[str] = field(default_factory=list)
    #: The base each cloud dispatch was told to start from (D16).
    assigned_base: dict[int, Base] = field(default_factory=dict)
    next_pr: int = 900

    def open_pr(self, *, head: str, title: str, body: str, base: Base) -> int:
        self.calls.append(f"open_pr({head})")
        self.opened.append({"head": head, "title": title, "body": body, "base": str(base)})
        self.next_pr += 1
        return self.next_pr

    def ensure_branch(self, *, base: Base) -> None:
        self.calls.append(f"ensure_branch({base})")
        self.ensured_branches.append(str(base))

    def close_issue(self, *, number: int) -> None:
        self.calls.append(f"close_issue({number})")
        self.state = RepoState(
            tuple(
                replace(issue, closed=True) if issue.number == number else issue
                for issue in self.state.issues
            )
        )

    def comment(self, *, number: int, body: str) -> None:
        self.calls.append(f"comment(#{number})")
        self.comments.append({"number": str(number), "body": body})

    def fetch_state(self) -> RepoState:
        self.calls.append("fetch_state()")
        return self.state

    def ensure_labels(self, labels: Sequence[str]) -> None:
        self.calls.append(f"ensure_labels({sorted(labels)})")
        self.ensured_labels |= set(labels)

    def create_issue(self, *, title: str, body: str, labels: Sequence[str]) -> int:
        self.calls.append(f"create_issue({title})")
        number = self.next_number
        self.next_number += 1
        self.state = RepoState(
            (
                *self.state.issues,
                IssueState(
                    number=number,
                    title=title,
                    body=body,
                    labels=tuple(labels),
                    closed=False,
                ),
            )
        )
        return number

    def update_issue(
        self,
        *,
        number: int,
        title: str,
        body: str,
        labels: Sequence[str],
        remove_labels: Sequence[str] = (),
    ) -> None:
        self.calls.append(f"update_issue({number})")
        self._replace(number, title=title, body=body, labels=tuple(labels))

    def assign_agent(self, *, number: int, node_id: str, base: Base = DEFAULT_BASE) -> None:
        self.calls.append(f"assign_agent({number})")
        self.assigned_base[number] = base
        # `replaceActorsForAssignable` replaces rather than appends, so a
        # second assignment of the same actor leaves the state unchanged —
        # which is exactly what makes concurrent passes safe.
        issue = self._issue(number)
        # GitHub keeps the assignment history whatever happens to the
        # assignment itself, which is what lets `attempts` be derived.
        self._replace(
            number,
            assignees=(AGENT_LOGIN,),
            dispatches=(*issue.dispatches, self.clock),
        )

    def unassign_agent(self, *, number: int, assignees: Sequence[str]) -> None:
        self.calls.append(f"unassign_agent({number})")
        issue = self._issue(number)
        remaining = tuple(name for name in issue.assignees if name not in set(assignees))
        # `dispatches` is deliberately untouched: releasing the lock must not
        # erase the fact that an attempt was spent, or the budget never bites.
        self._replace(number, assignees=remaining)

    def mark_ready(self, *, number: int) -> None:
        self.calls.append(f"mark_ready({number})")
        # The real mutation is idempotent — a PR already out of draft stays
        # out — and so is this: the double rewrites the flag rather than
        # counting, so a second pass over the same PR changes nothing.
        for issue in self.state.issues:
            replacement = tuple(
                replace(pr, draft=False) if pr.number == number else pr for pr in issue.open_prs
            )
            if replacement != issue.open_prs:
                self._replace(issue.number, open_prs=replacement)

    def merge_pr(self, *, number: int) -> None:
        self.calls.append(f"merge_pr({number})")
        # Models what a squash merge of a `Closes #N` pull request actually
        # does to the state the scheduler reads back: the PR stops being open
        # and the issue it closes is closed. Without the second half the
        # convergence test would pass for the wrong reason.
        for issue in self.state.issues:
            remaining = tuple(pr for pr in issue.open_prs if pr.number != number)
            if remaining != issue.open_prs:
                self._replace(issue.number, open_prs=remaining, closed=True)

    def edit_labels(
        self, *, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        self.calls.append(f"edit_labels({number}, +{sorted(add)}, -{sorted(remove)})")
        for issue in self.state.issues:
            if issue.number == number:
                labels = [label for label in issue.labels if label not in set(remove)]
                labels += [label for label in add if label not in labels]
                self._replace(number, labels=tuple(labels))
                return
        raise AssertionError(f"no issue numbered {number}")

    def _issue(self, number: int) -> IssueState:
        for issue in self.state.issues:
            if issue.number == number:
                return issue
        raise AssertionError(f"no issue numbered {number}")

    def _replace(self, number: int, **changes: object) -> None:
        self.state = RepoState(
            tuple(
                replace(issue, **changes) if issue.number == number else issue  # type: ignore[arg-type]
                for issue in self.state.issues
            )
        )
