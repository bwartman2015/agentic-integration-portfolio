"""STATE (concept 1, Memory & State): a snapshot of the current system, refreshed every iteration.

Memory holds PAST information (memory.py). State holds the CURRENT situation: an order's
status right now, today's DLQ depth, which consumers are paused. The loop re-reads state
before each THINK step, so the agent sees the effect of its own actions.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol

from .types import Percept


class StateProvider(Protocol):
    def snapshot(self, percept: Percept) -> dict[str, Any]: ...


class StaticState:
    """Fixed state, useful for tests and demos."""

    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state or {}

    def snapshot(self, percept: Percept) -> dict[str, Any]:
        return dict(self.state)


class CallableState:
    """Wrap any function (e.g. a Prometheus or ERP query) as a StateProvider."""

    def __init__(self, fn: Callable[[Percept], dict[str, Any]]):
        self.fn = fn

    def snapshot(self, percept: Percept) -> dict[str, Any]:
        return self.fn(percept)
