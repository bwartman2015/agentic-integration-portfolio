"""A real OPA for the gateway tests: the image's test stage copies in the pinned `opa` binary and the
project's policy/ directory, so these tests exercise the committed Rego, not a Python imitation of it.
One server per test process on loopback (the test container has no other network)."""
from __future__ import annotations

import atexit
import pathlib
import subprocess
import time
import urllib.request
from typing import Any
from unittest import mock

from ridgeline_gateway import server
from ridgeline_gateway.audit import MemoryAudit
from ridgeline_gateway.ratelimit import RateLimiter
from ridgeline_gateway.policy import OpaPolicy, PolicyConfig, build_policy

POLICY_DIR = pathlib.Path(__file__).resolve().parents[2] / "policy"
OPA_URL = "http://127.0.0.1:18181"
READ = ["ar.read", "crm.read", "erp.read", "shipping.read"]
REP = {"client_id": "cs-rep", "scopes": READ, "regions": ["Central"]}
LEAD = {"client_id": "cs-lead", "scopes": READ + ["crm.write", "crm.pii"], "regions": ["*"]}
NO_SCOPES = {"client_id": "p01-expiry-test", "scopes": [], "regions": []}
CRM_ONLY = {"client_id": "p01-crm-only-test", "scopes": ["crm.read"], "regions": ["Central"]}
_process: subprocess.Popen | None = None


def _stop() -> None:
    if _process is not None:
        _process.terminate()
        _process.wait(timeout=5)


def start_opa() -> str:
    """Start OPA once with the same files and flags as docker-compose.yml; returns its base URL."""
    global _process
    if _process is None:
        _process = subprocess.Popen(
            ["opa", "run", "--server", "--addr=127.0.0.1:18181", "--authorization=basic", "--log-level=error",
             str(POLICY_DIR / "gateway.rego"), str(POLICY_DIR / "system_authz.rego"), str(POLICY_DIR / "data.json")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(_stop)
        deadline = time.monotonic() + 15
        while True:
            try:
                with urllib.request.urlopen(f"{OPA_URL}/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                if time.monotonic() > deadline or _process.poll() is not None:
                    raise RuntimeError("the test OPA server did not start") from None
                time.sleep(0.05)
    return OPA_URL


def real_policy() -> OpaPolicy:
    return build_policy(PolicyConfig(start_opa(), 2.0))


def use_policy(test: Any, principal: dict[str, Any] | None = LEAD) -> None:
    """Install the real policy for one test and, for in-process calls (which carry no token), the
    verified identity the HTTP layer would have supplied."""
    server.set_policy(real_policy())
    test.addCleanup(server.set_policy, None)
    use_governance(test)
    if principal is not None:
        act_as(test, principal)


def use_governance(test: Any) -> MemoryAudit:
    """A working audit store and a fresh rate limiter for one test (as `test.audit`). Approvals stay
    unconfigured (every write refused) unless the test installs an approver."""
    test.audit = MemoryAudit()
    server.set_audit(test.audit)
    server.set_limiter(RateLimiter())
    server.set_approver(None)
    test.addCleanup(server.set_audit, None)
    test.addCleanup(server.set_limiter, None)
    test.addCleanup(server.set_approver, None)
    return test.audit


def act_as(test: Any, principal: dict[str, Any]) -> None:
    patcher = mock.patch.object(server, "current_principal", return_value=dict(principal))
    patcher.start()
    test.addCleanup(patcher.stop)
