"""`make console-check P=01`: one question through the web console, the way the page asks it. Passes when
the run is answered with citations, made its tool calls through the gateway, and the gateway's audit
rows for its trace id come back. Runs inside the console container (it has the console token). Like the
wire tests it writes audit rows, so it refuses unless the gateway's database is `test` or `local`."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

from ..a2a_check import SAFE_DATABASES, gateway_database
from ..ask import DEMO_QUESTION

BASE = os.environ.get("CONSOLE_URL", "http://localhost:8790").rstrip("/")


def call(path: str, body: dict | None = None) -> dict:
    headers = {"Accept": "application/json", "X-Console-Token": os.environ.get("CONSOLE_TOKEN", "")}
    data = None
    if body is not None:
        headers.update({"Content-Type": "application/json", "X-Ridgeline-Console": "1"})
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    database = gateway_database(os.environ.get("GATEWAY_HEALTH_URL", "http://gateway:8000/healthz"))
    if database not in SAFE_DATABASES:
        print(f"the gateway's database is `{database}`: this check writes audit rows, so it runs only on a test "
              "or local database (make up P=01 DB=test)", file=sys.stderr)
        return 2
    try:
        config = call("/api/config")
    except OSError as exc:
        print(f"no console at {BASE} ({type(exc).__name__}): start it with `make console-up P=01`", file=sys.stderr)
        return 1
    print(f"console up: roles {[r['id'] for r in config['roles']]}, models "
          f"{[m['id'] + ('' if m['available'] else ' (off)') for m in config['models']]}")
    run = call("/api/runs", {"role": "cs-rep", "model": "scripted", "question": DEMO_QUESTION})
    for _ in range(120):
        run = call(f"/api/runs/{run['id']}")
        if run["status"] != "running":
            break
        time.sleep(1)
    tools = [t["tool"] for t in run["tools"]]
    print(f"run {run['id']}: status={run['status']} tools={tools} citations={run['citations']} trace={run['trace_id']}")
    rows = call(f"/api/runs/{run['id']}/audit").get("rows", [])
    print(f"audit rows for the trace: {len(rows)} ({sorted({r['tool'] for r in rows})})")
    ok = (run["status"] == "answered" and run["citations"] and len(tools) >= 4 and run["trace_id"]
          and len(rows) >= len(tools))
    print("console check passed" if ok else "FAIL: expected an answered run with citations, tool calls and audit rows")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
