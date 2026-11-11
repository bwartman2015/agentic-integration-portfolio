"""A2A discovery: read a2a/registry.json and turn each specialist's Agent Card into a delegation Tool."""
import json
import os
import pathlib

from agentcore import A2AClient, Tool, remote_agent_tool

REGISTRY = pathlib.Path(__file__).resolve().parents[2] / "a2a" / "registry.json"


def discover_specialists(path: pathlib.Path = REGISTRY) -> list[Tool]:
    """The static A2A_TOKEN goes only to specialists that use it. A specialist marked
    "auth": "keycloak-client-credentials" (Project 01's copilot) gets no shared token: the Command Center
    will fetch a Keycloak token for its own client there (a later Project 08 milestone), so a token one
    specialist could replay at the gateway is never handed to another."""
    token = os.environ.get("A2A_TOKEN")
    entries = json.loads(path.read_text())
    return [remote_agent_tool(A2AClient(e["url"], bearer_token=None if e.get("auth") == "keycloak-client-credentials"
                                        else token), scope="delegate.mesh") for e in entries]
