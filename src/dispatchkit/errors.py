"""Issue and error types shared by the D1 parser and validator.

A graph file is the human review gate, so both halves report *every* problem
they can see in one pass rather than failing on the first one — a reviewer
should get the whole list, not one line at a time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GraphIssue:
    code: str  # stable, machine-greppable: "cycle", "dangling-dependency", ...
    where: str  # task id when known, else the plan or file name
    message: str

    def __str__(self) -> str:
        return f"{self.code}: [{self.where}] {self.message}"


class GraphError(Exception):
    """Raised when a graph cannot be parsed, or is used while invalid."""

    def __init__(self, issues: list[GraphIssue]) -> None:
        super().__init__("; ".join(str(issue) for issue in issues))
        self.issues = tuple(issues)
