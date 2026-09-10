"""`src/dispatchkit` is pure standard library, and stays that way.

The rule outlived its first reason. It began as a property of the unattended
job: the last place to resolve a fresh dependency tree is a process holding a
token that can assign work, and D13 removed that process. What is left is the
same argument moved one step closer to home — the scheduler now runs on a
person's workstation under *their* credential, which is if anything a worse
place to execute whatever the registry served this morning.

The test used to live in `tests/test_workflow.py` and was the reason that file
could not simply be deleted with the workflow.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_the_package_imports_no_third_party_module() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "dispatchkit"
    roots = set()
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])

    assert roots - {"dispatchkit", "__future__"} <= sys.stdlib_module_names
