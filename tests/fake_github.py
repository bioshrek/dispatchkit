"""In-memory GitHub double for the D3 replay tests.

Seeded from a `RepoState` (itself parsed from a recorded API fixture), it
applies mutations to its own store and can hand back a fresh `RepoState`. That
round trip is what makes the idempotency claim testable: apply, re-read, plan
again, and assert the second plan is empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from dispatchkit.gh_cli import AGENT_LOGIN
from dispatchkit.github import IssueState, RepoState


@dataclass
class FakeGitHub:
    state: RepoState = field(default_factory=lambda: RepoState(()))
    next_number: int = 100
    next_item: int = 1
    calls: list[str] = field(default_factory=list)
    ensured_labels: set[str] = field(default_factory=set)

    def fetch_state(self, *, plan: str) -> RepoState:
        self.calls.append(f"fetch_state({plan})")
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
                    project_item_id=None,
                    fields={},
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

    def add_project_item(self, *, issue_number: int) -> str:
        self.calls.append(f"add_project_item({issue_number})")
        item_id = f"PVTI_{self.next_item}"
        self.next_item += 1
        self._replace(issue_number, project_item_id=item_id)
        return item_id

    def set_project_field(self, *, item_id: str, field_name: str, value: str) -> None:
        self.calls.append(f"set_project_field({item_id}, {field_name})")
        for issue in self.state.issues:
            if issue.project_item_id == item_id:
                self._replace(issue.number, fields={**issue.fields, field_name: value})
                return
        raise AssertionError(f"no issue holds project item {item_id}")

    def assign_agent(self, *, number: int, node_id: str) -> None:
        self.calls.append(f"assign_agent({number})")
        # `replaceActorsForAssignable` replaces rather than appends, so a
        # second assignment of the same actor leaves the state unchanged —
        # which is exactly what makes concurrent passes safe.
        self._replace(number, assignees=(AGENT_LOGIN,))

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

    def _replace(self, number: int, **changes: object) -> None:
        self.state = RepoState(
            tuple(
                replace(issue, **changes) if issue.number == number else issue  # type: ignore[arg-type]
                for issue in self.state.issues
            )
        )
