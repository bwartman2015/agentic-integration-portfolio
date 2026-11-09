"""A2A (concept 8) and the building block for MULTI-AGENT SYSTEMS (concept 9).

Agents talk to agents: each agent publishes an Agent Card (its skills and endpoint) at a
well-known URL; other agents discover the card and exchange tasks, messages and artifacts
over plain HTTP + JSON-RPC.

This is a small, dependency-free implementation shaped after the A2A protocol
(agent card at /.well-known/agent-card.json, JSON-RPC method "message/send", text parts,
Task with status.state and artifacts). It is NOT a certified implementation: Project 06 has a
milestone to verify against the current A2A spec and, if useful, swap in the official SDK.
"""
from __future__ import annotations

import json
import threading
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .act import Tool
from .types import Principal

CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/a2a"


@dataclass
class AgentSkill:
    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)


@dataclass
class AgentCard:
    name: str
    description: str
    url: str
    version: str = "0.1.0"
    skills: list[AgentSkill] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=lambda: {"streaming": False})
    defaultInputModes: list[str] = field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = field(default_factory=lambda: ["text/plain"])
    securitySchemes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentCard":
        d = dict(d)
        d["skills"] = [AgentSkill(**s) for s in d.get("skills", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _task(answer: str, state: str, trace_id: str, context_id: str | None) -> dict[str, Any]:
    return {"kind": "task", "id": uuid.uuid4().hex, "contextId": context_id or uuid.uuid4().hex,
            "status": {"state": state},
            "artifacts": [{"artifactId": uuid.uuid4().hex, "name": "answer",
                           "parts": [{"kind": "text", "text": answer}]}],
            "metadata": {"trace_id": trace_id}}


def serve_agent(agent: Any, card: AgentCard, principal: Principal, host: str = "127.0.0.1",
                port: int = 0, bearer_token: str | None = None) -> ThreadingHTTPServer:
    """Expose an agentcore.Agent as an A2A-style endpoint. Returns a running server (daemon thread).
    port=0 picks a free port; card.url is updated to the real address."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == CARD_PATH:
                return self._send(200, card.to_dict())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != RPC_PATH:
                return self._send(404, {"error": "not found"})
            if bearer_token and self.headers.get("Authorization") != f"Bearer {bearer_token}":
                return self._send(401, {"error": "unauthorized"})
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            rid = req.get("id")
            if req.get("method") != "message/send":
                return self._send(200, {"jsonrpc": "2.0", "id": rid,
                                        "error": {"code": -32601, "message": "method not found"}})
            msg = req.get("params", {}).get("message", {})
            text = " ".join(p.get("text", "") for p in msg.get("parts", []) if p.get("kind") == "text").strip()
            if not text:
                return self._send(200, {"jsonrpc": "2.0", "id": rid,
                                        "error": {"code": -32602, "message": "no text part"}})
            out = agent.run(text, principal)
            state = {"answered": "completed", "escalated": "input-required", "rejected": "rejected"}[out.status]
            self._send(200, {"jsonrpc": "2.0", "id": rid,
                             "result": _task(out.answer, state, out.trace_id, msg.get("contextId"))})

    server = ThreadingHTTPServer((host, port), Handler)
    card.url = f"http://{host}:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class A2AClient:
    def __init__(self, base_url: str, bearer_token: str | None = None, timeout: float = 120):
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), bearer_token, timeout

    def card(self) -> AgentCard:
        with urllib.request.urlopen(self.base_url + CARD_PATH, timeout=self.timeout) as r:
            return AgentCard.from_dict(json.loads(r.read()))

    def send(self, text: str, context_id: str | None = None) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send",
                "params": {"message": {"role": "user", "messageId": uuid.uuid4().hex, "contextId": context_id,
                                       "parts": [{"kind": "text", "text": text}]}}}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base_url + RPC_PATH, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            resp = json.loads(r.read())
        if "error" in resp:
            raise RuntimeError(f"A2A error {resp['error']}")
        return resp["result"]


def task_text(task: dict[str, Any]) -> str:
    return "\n".join(p.get("text", "") for a in task.get("artifacts", []) for p in a.get("parts", [])
                     if p.get("kind") == "text")


def remote_agent_tool(client: A2AClient, *, scope: str | None = None, name: str | None = None) -> Tool:
    """Discover a remote agent from its card and expose it to a manager agent as a Tool."""
    card = client.card()
    skills = "; ".join(f"{s.name}: {s.description}" for s in card.skills)
    tool_name = name or "ask_" + "".join(c if c.isalnum() else "_" for c in card.name.lower())

    def ask(task: str) -> dict[str, Any]:
        t = client.send(task)
        return {"agent": card.name, "state": t["status"]["state"], "answer": task_text(t),
                "trace_id": t.get("metadata", {}).get("trace_id")}

    return Tool(tool_name, f"Delegate to remote agent '{card.name}': {card.description} Skills: {skills}",
                handler=ask, params={"task": "string"}, required=["task"], scope=scope,
                side_effect="read", category="external")
