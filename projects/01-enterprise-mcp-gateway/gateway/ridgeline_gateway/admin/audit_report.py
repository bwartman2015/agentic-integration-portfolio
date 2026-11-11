"""Print the gateway's audit report.

    make audit P=01                  # totals by outcome and reason, the 15 most recent calls, approvals
    make audit P=01 ARGS=--limit=50
    make audit P=01 ARGS=--trace=<trace id>   # only the calls one copilot run made (its x-request-id)

Runs in the one-off `ops` container. It gets a token as `p01-auditor`, an operator identity that holds
only the audit scope (no agent does), and reads GET /audit. Argument values are already masked.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from ..connectors.base import ConnectorError, HttpConnector
from .token import token_for


async def fetch_report(base_url: str, token: str, limit: int) -> dict[str, Any]:
    http = HttpConnector("The gateway", "gateway", base_url, 10.0)
    try:
        return await http.json("GET", f"{base_url}/audit?limit={limit}",
                               headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    finally:
        await http.aclose()


def only_trace(report: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """Keep the recent calls of one agent run: the copilot sends its trace id as x-request-id."""
    return {**report, "recent": [r for r in report.get("recent") or [] if r.get("trace_id") == trace_id]}


def render(report: dict[str, Any]) -> str:
    lines = [f"audit log: {report.get('total', 0)} call(s)",
             "by outcome: " + (", ".join(f"{k}={v}" for k, v in (report.get("by_outcome") or {}).items()) or "-"),
             "by reason:  " + (", ".join(f"{k}={v}" for k, v in (report.get("by_reason") or {}).items()) or "-"),
             "", f"{'time (UTC)':<20} {'client':<18} {'tool':<18} {'outcome':<18} {'reason':<22} {'trace':<14} args"]
    for row in report.get("recent") or []:
        lines.append(f"{str(row.get('at', ''))[:19]:<20} {str(row.get('client_id') or '-'):<18} "
                     f"{str(row.get('tool')):<18} {str(row.get('outcome')):<18} {str(row.get('reason') or '-'):<22} "
                     f"{str(row.get('trace_id') or '-')[:12]:<14} {json.dumps(row.get('args') or {}, sort_keys=True)[:90]}")
    approvals = report.get("approvals") or []
    if approvals:
        lines += ["", f"{'requested (UTC)':<20} {'request':<22} {'client':<12} {'account':<10} {'status':<12} approver"]
        for a in approvals:
            lines.append(f"{str(a.get('requested_at', ''))[:19]:<20} {str(a.get('request_id')):<22} "
                         f"{str(a.get('client_id') or '-'):<12} {str(a.get('account_id') or '-'):<10} "
                         f"{str(a.get('status')):<12} {a.get('approver') or '-'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the gateway audit report")
    parser.add_argument("--limit", type=int, default=15, help="how many recent calls to show (max 200)")
    parser.add_argument("--json", action="store_true", help="print the raw report")
    parser.add_argument("--trace", default="", help="only calls made with this trace id (searches the last 200)")
    args = parser.parse_args(argv)
    base_url = os.environ.get("GATEWAY_BASE_URL", "").rstrip("/")
    if not base_url:
        raise SystemExit("GATEWAY_BASE_URL must be set (run via `make audit`)")
    try:
        limit = 200 if args.trace else max(1, args.limit)
        report = asyncio.run(fetch_report(base_url, token_for("p01-auditor"), limit))
    except ConnectorError as exc:
        print(f"audit report failed: {exc}", file=sys.stderr)
        return 1
    if args.trace:
        report = only_trace(report, args.trace)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
