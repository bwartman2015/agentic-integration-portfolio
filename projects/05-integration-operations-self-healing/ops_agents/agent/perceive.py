"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input."""
from typing import Any

from agentcore import Percept, Principal


class IncidentPerceiver:
    """
    Normalize a Grafana/Prometheus alert webhook plus DLQ depth into an Incident Percept: dedupe by
    fingerprint, attach trace ids and sample DLQ messages, entities {flow, service, queue,
    failure_signals, business_impact}.

    Percept.kind = 'incident'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("IncidentPerceiver.perceive")
