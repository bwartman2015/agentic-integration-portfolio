"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input."""
from typing import Any

from agentcore import Percept, Principal


class PRContextPerceiver:
    """
    Collect PR diff, OpenAPI diff, Spectral findings and oasdiff breaking changes into a Percept
    with entities {pr_number, service, changed_endpoints, lint_findings, breaking_changes}. Docs-
    only PRs set entities.ai_needed = False so THINK can answer directly (skip).

    Percept.kind = 'pull_request'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("PRContextPerceiver.perceive")
