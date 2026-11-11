"""`make a2a-check P=01`: fetch the copilot's Agent Card over A2A, then send it a task as cs-rep with a
real Keycloak token. Passes when the card is served and the task comes back `completed` with a trace id.
Runs in the one-off copilot container (it holds the cs-rep client secret). Like the wire tests it makes
real, audited gateway calls, so it refuses unless the gateway's database is `test` or `local` (make up
P=01 DB=test): automated checks never write to the Neon main branch."""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from agentcore import A2AClient, task_text

from .agent.mcp_client import KeycloakTokens

QUESTION = "Where is order SO-104233 for Summit Contractors?"


SAFE_DATABASES = ("test", "local")


def gateway_database(health_url: str) -> str:
    try:
        with urllib.request.urlopen(health_url, timeout=5) as resp:
            return str(json.loads(resp.read()).get("database", "unknown"))
    except (OSError, ValueError):
        return "unreachable"


def main() -> int:
    database = gateway_database(os.environ.get("GATEWAY_HEALTH_URL", "http://gateway:8000/healthz"))
    if database not in SAFE_DATABASES:
        print(f"the gateway's database is `{database}`: this check writes audit rows, so it runs only on a test "
              "or local database (make up P=01 DB=test)", file=sys.stderr)
        return 2
    url = os.environ.get("A2A_URL", "http://a2a:8701")
    try:
        card = A2AClient(url).card()
    except OSError as exc:
        print(f"no A2A endpoint at {url} ({type(exc).__name__}): start it with `make a2a-up P=01`", file=sys.stderr)
        return 1
    print(f"card: {card.name} {card.version} at {card.url}; skills: {', '.join(s.id for s in card.skills)}")
    try:
        A2AClient(url).send(QUESTION)
        print("FAIL: a task without a token was accepted")
        return 1
    except Exception as exc:  # noqa: BLE001  expected: 401
        print(f"no token: refused ({type(exc).__name__})")
    task = A2AClient(url, bearer_token=KeycloakTokens.for_role("cs-rep")(), timeout=600).send(QUESTION)
    state, trace = task["status"]["state"], (task.get("metadata") or {}).get("trace_id")
    print(f"as cs-rep: state={state} trace_id={trace}")
    print("answer: " + task_text(task)[:300])
    ok = state == "completed" and bool(trace)
    print("a2a check passed" if ok else "FAIL: expected state completed with a trace id")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
