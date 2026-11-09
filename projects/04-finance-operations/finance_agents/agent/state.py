"""STATE (Memory & State): the current system snapshot, re-read before every THINK step.

Accounting period status (open / closing / closed), today's exception queue size and unapplied cash total.
"""
from typing import Any

from agentcore import Percept


class CurrentStateProvider:
    def snapshot(self, percept: Percept) -> dict[str, Any]:
        # TODO(Milestone 0): query the mock systems for the live snapshot described above.
        raise NotImplementedError("CurrentStateProvider.snapshot")


def get_state() -> CurrentStateProvider:
    return CurrentStateProvider()
