"""Local web console for the CS copilot (Milestone 8): ask a question as a rep or a lead and watch the run.

    make console-up P=01     http://localhost:8790   (published on 127.0.0.1 only)
    make console-check P=01  one run through the console on a test stack

A question runs the real copilot exactly like `make ask`: tools and state through the MCP gateway with the
chosen role's Keycloak token, long-term memory in pgvector, the chosen model (scripted, the free local
model, or the optional hosted one when configured). The page polls the run and shows each step as it
finishes: plan, tool calls (allowed or denied, with the reason), memory recall, the verifier's check, the
answer with clickable citations, and the gateway's audit rows for the run's trace id.

Operator view. Besides the two agent identities, this service holds two operator credentials that no agent
container has: the auditor identity (p01-auditor, scope audit.read) for the audit rows, and the approval
operator token for the "waiting for approval" state and its link to the n8n form. The agent itself never
sees either: they are used only by this server, after or beside the run.

Access: a console token (CONSOLE_TOKEN in .env, Jupyter-style). `make console-up` prints a one-time link
http://localhost:8790/?token=...; opening it sets an HttpOnly, SameSite=Strict cookie and every /api call
needs that cookie (or the token in X-Console-Token, for `make console-check` inside this container). So
neither another website nor another container on the Docker networks (an agent container, a model server)
can drive the console or reach the operator credentials it holds. Also: published on 127.0.0.1 only, an
allowed Host is required, a write (POST) needs the X-Ridgeline-Console header and, when the browser sends
one, an allowed Origin; every response carries a strict Content-Security-Policy (no inline script, no
third-party code). The agent code runs in this process, but its model and tools never receive the operator
credentials: only this server's own audit and approval lookups use them.
"""
from __future__ import annotations

import hmac
import json
import os
import pathlib
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agentcore import Tracer
from agentcore.observe import Span

from ..agent.build import ROLE_SCOPES, ROLE_REGIONS, build_cs_copilot, principal_for_role
from ..agent.knowledge import get_retriever
from ..agent.mcp_client import KeycloakTokens, McpToolSource
from ..agent.reason import get_llm, hosted_model_configured, local_model_available
from ..ask import known_questions, script_for

STATIC = pathlib.Path(__file__).resolve().parent / "static"
CITATION = re.compile(r"\[([^\[\]\s]+\.md#\d+)\]")
MODELS = {"scripted": "Scripted (replays a plan for a known question)", "local": "Local: Qwen2.5-1.5B (free, slow on a CPU)",
          "hosted": "Hosted: Claude (optional, paid)"}
PROVIDER = {"scripted": "scripted", "local": "local", "hosted": "anthropic"}
ROLE_LABELS = {"cs-rep": "cs-rep: Central-region rep (read only)", "cs-lead": "cs-lead: all regions, may propose cases"}
MAX_QUESTION = 4000
MAX_ACTIVE = 2
MAX_KEPT = 50
SHOWN_ATTRS = ("tool", "ok", "error", "hits", "cites", "active", "decision", "steps", "achieved", "reason", "status")


# ------------------------------------------------------------------ runs
@dataclass
class Run:
    id: str
    role: str
    model: str
    question: str
    status: str = "running"            # running | answered | escalated | rejected | error
    started: float = field(default_factory=time.time)
    finished: float | None = None
    trace_id: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    recalled: list[dict[str, Any]] = field(default_factory=list)
    approval: dict[str, Any] | None = None
    iterations: int = 0
    cost_usd: float = 0.0
    error: str | None = None

    def view(self) -> dict[str, Any]:
        data = asdict(self)
        data["elapsed_s"] = round((self.finished or time.time()) - self.started, 1)
        return data


class RunSink:
    """A tracer sink that turns each finished span into a step the page can show while the run goes on."""

    def __init__(self, run: Run):
        self.run = run

    def export(self, span: Span) -> None:
        run = self.run
        run.trace_id = run.trace_id or span.trace_id
        attrs = {k: span.attrs[k] for k in SHOWN_ATTRS if span.attrs.get(k) not in (None, "", [])}
        if len(run.steps) < 300:
            run.steps.append({"name": span.name, "layer": span.layer, "ms": round(span.latency_ms, 1),
                              "status": span.status, "attrs": _jsonable(attrs)})
        if span.name.startswith("act."):
            run.tools.append({"tool": span.name.removeprefix("act."), "ok": bool(span.attrs.get("ok")),
                              "ms": round(span.latency_ms, 1), "error": _clean_error(span.attrs.get("error"))})


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _clean_error(error: Any) -> str | None:
    if not isinstance(error, str) or not error:
        return None
    return error[error.find("Access denied"):] if "Access denied" in error else error.split(": ", 1)[-1][:300]


def _last_result(history: list[dict[str, str]], tool: str) -> Any:
    prefix = f"Result of {tool}: "
    for message in reversed(history):
        content = message.get("content", "")
        if content.startswith(prefix):
            try:
                return json.loads(content[len(prefix):])
            except ValueError:
                return None
    return None


# ------------------------------------------------------------------ the gateway, as an operator
class Gateway:
    """Operator reads from the gateway: the audit rows of one trace (auditor token) and the approval
    waiting for one trace (operator token). Both are plain HTTP on the internal network."""

    def __init__(self, base_url: str, auditor: Callable[[], str] | None, operator_token: str, timeout_s: float = 10):
        self.base_url, self.auditor, self.operator_token, self.timeout_s = base_url.rstrip("/"), auditor, operator_token, timeout_s

    def _get(self, path: str, token: str) -> Any:
        req = urllib.request.Request(self.base_url + path, headers={"Authorization": f"Bearer {token}",
                                                                    "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read())

    def audit_rows(self, trace_id: str) -> list[dict[str, Any]]:
        if self.auditor is None:
            raise RuntimeError("no auditor identity configured")
        report = self._get("/audit?limit=200", self.auditor())
        keep = ("at", "client_id", "tool", "outcome", "reason", "args", "latency_ms", "approval_id")
        return [{k: r.get(k) for k in keep} for r in report.get("recent") or [] if r.get("trace_id") == trace_id]

    def approval_for(self, trace_id: str) -> dict[str, Any] | None:
        if not self.operator_token:
            return None
        for p in (self._get("/approvals/pending", self.operator_token) or {}).get("pending") or []:
            if p.get("trace_id") == trace_id:
                url = p.get("approval_url") if str(p.get("approval_url") or "").startswith(("http://", "https://")) else None
                return {"state": "waiting", "request_id": p.get("request_id"), "url": url,
                        "expires_in_s": p.get("expires_in_s"), "subject": p.get("subject")}
        return None


# ------------------------------------------------------------------ the console
class Console:
    def __init__(self, *, gateway_url: str, gateway: Gateway, tokens: Callable[[str], Callable[[], str]],
                 llm_for: Callable[[str, str], Any] | None = None, builder: Callable[..., Any] = build_cs_copilot,
                 local_available: Callable[[], bool] | None = None, n8n_url: str = "http://localhost:5678"):
        self.gateway_url, self.gateway, self.tokens, self.builder = gateway_url, gateway, tokens, builder
        self.llm_for = llm_for or _default_llm
        self.local_available = local_available or local_model_available
        self.n8n_url = n8n_url
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()
        self.chunks = {c.cite: {"source": c.source, "text": c.text} for c in get_retriever().chunks}
        self._approval_checked: dict[str, float] = {}

    # ---- configuration for the page
    def config(self) -> dict[str, Any]:
        available = {"scripted": True, "local": self.local_available(), "hosted": hosted_model_configured()}
        return {"roles": [{"id": r, "label": ROLE_LABELS[r], "regions": list(ROLE_REGIONS[r]),
                           "scopes": sorted(ROLE_SCOPES[r])} for r in ROLE_SCOPES],
                "models": [{"id": m, "label": label, "available": available[m]} for m, label in MODELS.items()],
                "examples": list(known_questions()), "n8n_url": self.n8n_url}

    # ---- runs
    def start(self, role: str, model: str, question: str) -> Run:
        question = question.strip()
        if role not in ROLE_SCOPES:
            raise ValueError("unknown role")
        if model not in MODELS:
            raise ValueError("unknown model")
        if not question or len(question) > MAX_QUESTION:
            raise ValueError(f"ask a question of 1 to {MAX_QUESTION} characters")
        if model == "scripted" and script_for(question) is None:
            raise ValueError("the scripted model only knows the example questions; pick one, or use the local model")
        if model == "hosted" and not hosted_model_configured():
            raise ValueError("the hosted model is not configured (it is optional and paid)")
        with self.lock:
            if sum(r.status == "running" for r in self.runs.values()) >= MAX_ACTIVE:
                raise RuntimeError("two questions are already running; wait for one to finish")
            run = Run(uuid.uuid4().hex[:12], role, model, question)
            self.runs[run.id] = run
            for old in sorted(self.runs.values(), key=lambda r: r.started)[:-MAX_KEPT]:
                if old.status != "running":
                    del self.runs[old.id]
                    self._approval_checked.pop(old.id, None)
        threading.Thread(target=self._execute, args=(run,), daemon=True, name=f"run-{run.id}").start()
        return run

    def _execute(self, run: Run) -> None:
        """Run the copilot, then publish the results with `status` set last (under the lock), so a poll
        never sees a finished run whose answer, citations or approval are not filled in yet."""
        final: dict[str, Any]
        try:
            llm = self.llm_for(run.model, run.question)
            source = McpToolSource(self.gateway_url, self.tokens(run.role))
            agent = self.builder(llm, role=run.role, source=source)
            agent.tracer = Tracer(*agent.tracer.sinks, RunSink(run))
            out = agent.run(run.question, principal_for_role(run.role))
            final = {"answer": out.answer, "iterations": out.iterations,
                     "cost_usd": round(out.cost_usd, 5) if run.model == "hosted" else 0.0,
                     "trace_id": out.trace_id or run.trace_id,
                     "citations": sorted(set(CITATION.findall(out.answer))),
                     "recalled": [{"kind": m.kind, "score": m.score, "text": m.text.splitlines()[0][:200],
                                   "from_trace": str(m.meta.get("trace_id", ""))[:12]}
                                  for m in getattr(agent.memory, "last_recall", None) or []],
                     "status": out.status}
            if any(t["tool"] == "create_case" for t in run.tools):
                result = _last_result(out.history, "create_case")
                if isinstance(result, dict) and "error" not in result:
                    final["approval"] = {"state": "approved", "case_id": result.get("case_id"),
                                         "approved_by": (result.get("approval") or {}).get("approved_by")}
                else:
                    reason = _clean_error((result or {}).get("error") if isinstance(result, dict) else None)
                    final["approval"] = {"state": "not_created", "reason": reason or "the request was refused"}
        except Exception as exc:  # noqa: BLE001  any failure becomes a visible (generic) error on the page
            print(f"console run {run.id} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            final = {"status": "error", "error": f"{type(exc).__name__}: the run could not complete (details in the console log)"}
        with self.lock:
            status = final.pop("status")
            for key, value in final.items():
                setattr(run, key, value)
            run.finished = time.time()
            run.status = status
            self._approval_checked.pop(run.id, None)

    def get(self, run_id: str) -> Run | None:
        run = self.runs.get(run_id)
        if run is not None and run.status == "running" and run.trace_id:
            self._refresh_approval(run)
        return run

    def _refresh_approval(self, run: Run) -> None:
        """While the run waits on a write, show the human step: at most one gateway lookup per 2 s."""
        now = time.time()
        if now - self._approval_checked.get(run.id, 0) < 2:
            return
        self._approval_checked[run.id] = now
        try:
            waiting = self.gateway.approval_for(run.trace_id or "")
        except (OSError, ValueError):
            return
        with self.lock:
            if run.status != "running":
                return                         # the run finished meanwhile: its own result stands
            if waiting:
                run.approval = waiting
            elif run.approval and run.approval.get("state") == "waiting":
                run.approval = {"state": "decided", "note": "a decision was made; the copilot is finishing"}

    def audit(self, run_id: str) -> list[dict[str, Any]]:
        run = self.runs.get(run_id)
        if run is None or not run.trace_id:
            return []
        return self.gateway.audit_rows(run.trace_id)

    def citation(self, cite: str) -> dict[str, Any] | None:
        return self.chunks.get(cite)


def _default_llm(model: str, question: str) -> Any:
    return get_llm(script=script_for(question) if model == "scripted" else None, provider=PROVIDER[model])


# ------------------------------------------------------------------ HTTP
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
WRITE_HEADER = "x-ridgeline-console"
TOKEN_HEADER = "x-console-token"
COOKIE = "ridgeline_console"
LOCKED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Console locked</title>
<link rel="stylesheet" href="/static/console.css"></head><body><main class="layout"><section class="card">
<h2>This console needs its link</h2><p>Run <code>make console-up P=01</code> and open the link it prints
(http://localhost:8790/?token=...). It signs this browser in for the session.</p></section></main></body></html>"""


class SecurityHeaders:
    """Pure ASGI middleware: the security headers on every response, static files included."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.decode().lower() not in {h.lower() for h in SECURITY_HEADERS}]
                headers += [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, send_with_headers)


def create_app(console: Console, allowed_hosts: list[str], allowed_origins: list[str], token: str) -> Starlette:
    if len(token) < 32:
        raise ValueError("CONSOLE_TOKEN must be at least 32 characters (make up P=01 generates it)")

    def secured(response: Response) -> Response:
        return response                       # the SecurityHeaders middleware adds the headers

    def authorized(request: Request) -> bool:
        presented = request.cookies.get(COOKIE) or request.headers.get(TOKEN_HEADER) or ""
        return hmac.compare_digest(presented.encode(), token.encode())

    def locked() -> Response:
        return JSONResponse({"error": "open the console link that `make console-up` prints"}, status_code=401)

    def check_write(request: Request) -> Response | None:
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return secured(JSONResponse({"error": "origin not allowed"}, status_code=403))
        if request.headers.get(WRITE_HEADER) != "1":
            return secured(JSONResponse({"error": "missing console header"}, status_code=403))
        return None

    async def index(request: Request) -> Response:
        offered = request.query_params.get("token")
        if offered is not None:
            if not hmac.compare_digest(offered.encode(), token.encode()):
                return HTMLResponse(LOCKED_PAGE, status_code=401)
            # Sign this browser in, then drop the token from the address bar.
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
            return response
        if not authorized(request):
            return HTMLResponse(LOCKED_PAGE, status_code=401)
        return secured(FileResponse(STATIC / "index.html", media_type="text/html"))

    async def health(request: Request) -> Response:
        return secured(JSONResponse({"status": "ok"}))

    async def config(request: Request) -> Response:
        if not authorized(request):
            return locked()
        return secured(JSONResponse(console.config()))

    async def start(request: Request) -> Response:
        if not authorized(request):
            return locked()
        refused = check_write(request)
        if refused is not None:
            return refused
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("send a JSON object")
            run = console.start(str(body.get("role", "")), str(body.get("model", "")), str(body.get("question", "")))
        except ValueError as exc:
            return secured(JSONResponse({"error": str(exc)}, status_code=400))
        except RuntimeError as exc:
            return secured(JSONResponse({"error": str(exc)}, status_code=429))
        return secured(JSONResponse(run.view(), status_code=202))

    async def get_run(request: Request) -> Response:
        if not authorized(request):
            return locked()
        run = console.get(request.path_params["run_id"])
        if run is None:
            return secured(JSONResponse({"error": "no such run"}, status_code=404))
        return secured(JSONResponse(run.view()))

    async def audit(request: Request) -> Response:
        if not authorized(request):
            return locked()
        try:
            rows = console.audit(request.path_params["run_id"])
        except (OSError, ValueError, RuntimeError) as exc:
            return secured(JSONResponse({"error": f"the audit report is unavailable ({type(exc).__name__})"},
                                        status_code=502))
        return secured(JSONResponse({"rows": _jsonable(rows)}))

    async def citation(request: Request) -> Response:
        if not authorized(request):
            return locked()
        chunk = console.citation(request.path_params["cite"])
        if chunk is None:
            return secured(JSONResponse({"error": "unknown citation"}, status_code=404))
        return secured(JSONResponse(chunk))

    routes = [Route("/", index), Route("/api/health", health), Route("/api/config", config),
              Route("/api/runs", start, methods=["POST"]), Route("/api/runs/{run_id:str}", get_run),
              Route("/api/runs/{run_id:str}/audit", audit), Route("/api/citations/{cite:str}", citation),
              Mount("/static", app=StaticFiles(directory=STATIC), name="static")]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders),
                                                Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)])


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def main() -> None:
    import uvicorn
    env = os.environ
    token_url = env.get("OIDC_TOKEN_URL", "")
    auditor_secret = env.get("P01_AUDITOR_CLIENT_SECRET", "")
    auditor = KeycloakTokens("p01-auditor", auditor_secret, token_url) if auditor_secret and token_url else None
    agent_tokens: dict[str, KeycloakTokens] = {}

    def tokens(role: str) -> KeycloakTokens:
        if role not in agent_tokens:
            agent_tokens[role] = KeycloakTokens.for_role(role)
        return agent_tokens[role]

    console = Console(gateway_url=env.get("GATEWAY_URL", "http://gateway:8000/mcp"),
                      gateway=Gateway(env.get("GATEWAY_BASE_URL", "http://gateway:8000"), auditor,
                                      env.get("APPROVAL_OPERATOR_TOKEN", "")),
                      tokens=tokens, n8n_url=env.get("N8N_PUBLIC_URL", "http://localhost:5678"))
    port = int(env.get("CONSOLE_PORT", "8790"))
    app = create_app(console, _split(env.get("CONSOLE_ALLOWED_HOSTS", "localhost,127.0.0.1")),
                     _split(env.get("CONSOLE_ALLOWED_ORIGINS", f"http://localhost:{port},http://127.0.0.1:{port}")),
                     env.get("CONSOLE_TOKEN", ""))
    uvicorn.run(app, host=env.get("CONSOLE_HOST", "0.0.0.0"), port=8790, log_level="warning", proxy_headers=False,
                server_header=False)


if __name__ == "__main__":
    main()
