"""A2A discovery: read a2a/registry.json and turn each specialist's Agent Card into a delegation Tool."""
import json
import os
import pathlib

from agentcore import A2AClient, Tool, remote_agent_tool

REGISTRY = pathlib.Path(__file__).resolve().parents[2] / "a2a" / "registry.json"


def discover_specialists(path: pathlib.Path = REGISTRY) -> list[Tool]:
    token = os.environ.get("A2A_TOKEN")
    entries = json.loads(path.read_text())
    return [remote_agent_tool(A2AClient(e["url"], bearer_token=token), scope="delegate.mesh") for e in entries]
