"""STATE (Memory & State): the current system snapshot, re-read before every THINK step.

The dispute case: which specialists have answered, their findings so far, and open questions.
"""
from typing import Any

from agentcore import Percept


class CurrentStateProvider:
    def snapshot(self, percept: Percept) -> dict[str, Any]:
        # TODO(Milestone 0): query the mock systems for the live snapshot described above.
        raise NotImplementedError("CurrentStateProvider.snapshot")


def get_state() -> CurrentStateProvider:
    return CurrentStateProvider()
