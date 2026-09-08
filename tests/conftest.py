"""Shared pytest fixtures and gates (R4).

- Tiers 1 (`unit`) and 2 (`replay`) must never touch the network. We enforce
  this globally by monkeypatching `socket.socket` to raise, and only lift the
  block for tests explicitly marked `@pytest.mark.live`.
- `pytest.ini` / `[tool.pytest.ini_options]` deselects `live` by default
  (`addopts = "-m 'not live'"`), so this fixture is belt-and-braces: even if
  someone runs `pytest -m live` alongside tier 1/2 tests in the same session,
  only the tests actually marked `live` get network access.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

_real_socket = socket.socket


class NetworkBlockedError(RuntimeError):
    """Raised when a tier-1/2 test attempts to open a network socket."""


def _blocked_socket(*args: object, **kwargs: object) -> None:
    raise NetworkBlockedError(
        "Network access is blocked in this test tier. Mark the test "
        "`@pytest.mark.live` if it must reach a real provider."
    )


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("live") is not None:
        yield
        return

    socket.socket = _blocked_socket  # type: ignore[assignment, misc]
    try:
        yield
    finally:
        socket.socket = _real_socket  # type: ignore[misc]
