"""STATE (Memory & State): the current system snapshot, re-read before every THINK step.

Live health of the affected flow: error rate, throughput, DLQ depth vs baseline, consumers paused (refreshed each iteration to verify fixes).
"""
from typing import Any

from agentcore import Percept


class CurrentStateProvider:
    def snapshot(self, percept: Percept) -> dict[str, Any]:
        # TODO(Milestone 0): query the mock systems for the live snapshot described above.
        raise NotImplementedError("CurrentStateProvider.snapshot")


def get_state() -> CurrentStateProvider:
    return CurrentStateProvider()
