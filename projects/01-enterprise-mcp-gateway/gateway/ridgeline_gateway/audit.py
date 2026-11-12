"""Audit (Milestone 5): one durable row per tool call, allowed or denied, with the reason.

The sink is Postgres (db/audit.sql; db/init.sh applies it locally). The gateway's database role may INSERT and
SELECT on `audit_log` but not UPDATE or DELETE, so the gateway cannot rewrite its own history. Approval
requests have their own table: a row is written before a human is asked, and the write tool runs only
after that row exists, so no approved write can happen without a durable record.

Fail closed: if a read cannot be audited, its result is not returned (`audit_unavailable`). Argument
values are stored masked (pii.audit_args); results are never stored.

Where the store is: the local `db` container by default (AUDIT_DB_HOST and friends). Setting
DATABASE_URL (for example a Neon connection string) uses that database instead. A non-local host must
use TLS (`sslmode=require`, `verify-ca` or `verify-full`), or the gateway refuses to start. The URL is a
secret: it is never logged, printed or shown in an error.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

AUDIT_UNAVAILABLE = "audit_unavailable"
APPROVAL_STATUSES = ("pending", "approved", "rejected", "timed_out", "unavailable")
MAX_REPORT_ROWS = 200
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# Hosts that never leave this machine or the Compose network: TLS is optional only for these.
LOCAL_HOSTS = frozenset({"db", "localhost", "127.0.0.1", "::1", "host.docker.internal"})
TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
# libpq options asyncpg does not implement; it would send them to the server as settings, which fails.
# channel_binding is one (Neon's console adds it); the connection is still TLS with SCRAM authentication.
_DROPPED_DSN_OPTIONS = frozenset({"channel_binding"})
# verify-ca / verify-full need a root certificate. asyncpg does not read the system store by itself, so
# a URL that names none uses the image's CA bundle (Debian's ca-certificates).
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def _database_url(raw: str) -> tuple[str, str, list[str]]:
    """Validate DATABASE_URL. Returns (dsn for asyncpg, host for the startup log, errors). Error texts
    name the variable and the rule, never any part of the value."""
    try:
        parts = urlsplit(raw)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return "", "", ["DATABASE_URL is not a valid URL"]
    errors: list[str] = []
    if parts.scheme not in ("postgres", "postgresql"):
        errors.append("DATABASE_URL must start with postgresql://")
    if not host or not _HOST.match(host) or not parts.username or not parts.path.strip("/"):
        errors.append("DATABASE_URL must name a user, a host and a database")
    query = parse_qsl(parts.query, keep_blank_values=True)
    sslmode = dict(query).get("sslmode", "")
    if host not in LOCAL_HOSTS and sslmode not in TLS_MODES:
        errors.append("DATABASE_URL must use TLS for a remote database: add sslmode=require (or verify-full)")
    kept = [(k, v) for k, v in query if k not in _DROPPED_DSN_OPTIONS]
    if sslmode in ("verify-ca", "verify-full") and "sslrootcert" not in dict(query):
        kept.append(("sslrootcert", SYSTEM_CA_BUNDLE))
    dsn = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))
    # The log names a hosted database only by its provider domain, never by its endpoint name.
    shown = host if host in LOCAL_HOSTS else "*." + ".".join(host.split(".")[-2:])
    return dsn, shown, errors

AUDIT_COLUMNS = ("client_id", "tool", "outcome", "reason", "scopes", "regions", "args", "latency_ms",
                 "backend", "upstream_status", "request_id", "trace_id", "approval_id", "pii_masked")


class AuditUnavailable(RuntimeError):
    """The audit store did not accept a record. Callers treat this as "deny"."""


class AuditConfigError(RuntimeError):
    """Raised at startup. Messages name variables, never values."""


@dataclass(frozen=True)
class AuditConfig:
    host: str
    database: str
    user: str
    password: str = field(repr=False)
    port: int = 5432
    timeout_s: float = 3.0
    dsn: str = field(default="", repr=False)     # set when DATABASE_URL is used; never printed

    @property
    def source(self) -> str:
        return "database_url" if self.dsn else "local"

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "AuditConfig":
        get = lambda name, default="": (env.get(name) or default).strip()   # noqa: E731
        errors: list[str] = []
        raw_timeout = get("AUDIT_TIMEOUT_S", "3")
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = -1.0
        timeout_error = [] if 0 < timeout <= 10 else ["AUDIT_TIMEOUT_S must be a number between 0 and 10"]
        database_url = get("DATABASE_URL")
        if database_url:
            dsn, host, url_errors = _database_url(database_url)
            if url_errors or timeout_error:
                raise AuditConfigError("invalid audit configuration (the gateway never runs without an audit "
                                       "store): " + "; ".join(url_errors + timeout_error))
            return AuditConfig(host, "", "", "", 0, timeout, dsn)
        host, database, user = get("AUDIT_DB_HOST"), get("AUDIT_DB_NAME", "ridgeline"), get("AUDIT_DB_USER", "gateway_audit")
        password = env.get("AUDIT_DB_PASSWORD") or ""
        if not _HOST.match(host):
            errors.append("AUDIT_DB_HOST must be a host name such as db")
        for name, value in (("AUDIT_DB_NAME", database), ("AUDIT_DB_USER", user)):
            if not _IDENT.match(value):
                errors.append(f"{name} must be a plain lower-case identifier")
        if len(password) < 16:
            errors.append("AUDIT_DB_PASSWORD must be set (at least 16 characters)")
        raw_port = get("AUDIT_DB_PORT", "5432")
        port = int(raw_port) if raw_port.isdigit() else 0
        if not 1 <= port <= 65535:
            errors.append("AUDIT_DB_PORT must be a port number")
        errors += timeout_error
        if errors:
            raise AuditConfigError("invalid audit configuration (the gateway never runs without an audit "
                                   "store): " + "; ".join(errors))
        return AuditConfig(host, database, user, password, port, timeout)


class AuditSink(Protocol):
    async def write(self, record: Mapping[str, Any]) -> None: ...

    async def approval_requested(self, row: Mapping[str, Any]) -> None: ...

    async def approval_decided(self, request_id: str, status: str, approver: str | None) -> None: ...

    async def report(self, limit: int = 50) -> dict[str, Any]: ...

    async def close_orphaned_approvals(self) -> list[str]: ...

    async def environment(self) -> str | None: ...

    async def aclose(self) -> None: ...


ENVIRONMENTS = frozenset({"main", "test", "local"})
SELECT_ENVIRONMENT = "SELECT name FROM public.ridgeline_environment LIMIT 1"


def _row(record: Mapping[str, Any]) -> dict[str, Any]:
    """The audit columns of a call record; anything else in the record is ignored."""
    row = {c: record.get(c) for c in AUDIT_COLUMNS}
    row["scopes"] = [str(s) for s in (record.get("scopes") or [])]
    row["regions"] = [str(r) for r in (record.get("regions") or [])]
    row["args"] = record.get("args") or {}
    row["pii_masked"] = int(record.get("pii_masked") or 0)
    return row


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_outcome: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
        if r.get("reason"):
            by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    return {"by_outcome": dict(sorted(by_outcome.items())), "by_reason": dict(sorted(by_reason.items()))}


class UnavailableAudit:
    """What the gateway uses until an audit store is configured: nothing can be recorded, so every
    read is refused and every approval request fails."""

    async def write(self, record: Mapping[str, Any]) -> None:
        raise AuditUnavailable("no audit store configured")

    async def approval_requested(self, row: Mapping[str, Any]) -> None:
        raise AuditUnavailable("no audit store configured")

    async def approval_decided(self, request_id: str, status: str, approver: str | None) -> None:
        raise AuditUnavailable("no audit store configured")

    async def report(self, limit: int = 50) -> dict[str, Any]:
        raise AuditUnavailable("no audit store configured")

    async def close_orphaned_approvals(self) -> list[str]:
        raise AuditUnavailable("no audit store configured")

    async def environment(self) -> str | None:
        raise AuditUnavailable("no audit store configured")

    async def aclose(self) -> None:
        return None


class MemoryAudit:
    """In-process sink with the same behaviour as the Postgres one. Used by the gateway's tests."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self.fail = False                       # tests flip this to simulate the store going away

    def _check(self) -> None:
        if self.fail:
            raise AuditUnavailable("audit store unreachable")

    async def write(self, record: Mapping[str, Any]) -> None:
        self._check()
        self.rows.append({"id": len(self.rows) + 1, "at": datetime.now(timezone.utc).isoformat(), **_row(record)})

    async def approval_requested(self, row: Mapping[str, Any]) -> None:
        self._check()
        self.approvals[row["request_id"]] = {**row, "status": "pending", "approver": None, "decided_at": None,
                                             "requested_at": datetime.now(timezone.utc).isoformat()}

    async def approval_decided(self, request_id: str, status: str, approver: str | None) -> None:
        self._check()
        assert status in APPROVAL_STATUSES
        row = self.approvals.get(request_id)
        if row is None or row["status"] != "pending":
            raise AuditUnavailable("the approval was no longer pending")
        row.update(status=status, approver=approver, decided_at=datetime.now(timezone.utc).isoformat())

    async def report(self, limit: int = 50) -> dict[str, Any]:
        self._check()
        limit = max(1, min(int(limit), MAX_REPORT_ROWS))
        return {"total": len(self.rows), **_summarize(self.rows), "recent": list(reversed(self.rows))[:limit],
                "approvals": list(reversed(list(self.approvals.values())))[:limit]}

    async def close_orphaned_approvals(self) -> list[str]:
        self._check()
        closed = [rid for rid, a in self.approvals.items() if a["status"] == "pending"]
        for rid in closed:
            self.approvals[rid].update(status="unavailable", decided_at=datetime.now(timezone.utc).isoformat())
        return closed

    async def environment(self) -> str | None:
        self._check()
        return getattr(self, "stamp", None)

    async def aclose(self) -> None:
        return None


INSERT_AUDIT = (f"INSERT INTO audit_log ({', '.join(AUDIT_COLUMNS)}) VALUES "
                f"({', '.join(f'${i}' for i in range(1, len(AUDIT_COLUMNS) + 1))})")
INSERT_APPROVAL = ("INSERT INTO approvals (request_id, client_id, tool, account_id, subject, trace_id) "
                   "VALUES ($1, $2, $3, $4, $5, $6)")
UPDATE_APPROVAL = ("UPDATE approvals SET status = $2, approver = $3, decided_at = now() "
                   "WHERE request_id = $1 AND status = 'pending'")
# A gateway that stops while a human is deciding leaves its request pending; no process can resolve it
# any more. One gateway runs per database, so at startup every pending request is such an orphan.
CLOSE_ORPHANED = ("UPDATE approvals SET status = 'unavailable', decided_at = now() "
                  "WHERE status = 'pending' RETURNING request_id")
SELECT_RECENT = (f"SELECT id, at, {', '.join(AUDIT_COLUMNS)} FROM audit_log ORDER BY id DESC LIMIT $1")
SELECT_APPROVALS = ("SELECT request_id, requested_at, client_id, tool, account_id, subject, status, approver, "
                    "decided_at FROM approvals ORDER BY requested_at DESC LIMIT $1")


def _jsonable(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            value = value.isoformat()
        elif key == "args" and isinstance(value, str):
            value = json.loads(value)
        out[key] = value
    return out


@dataclass
class PostgresAudit:
    """asyncpg-backed sink. Every statement is parameterized and bounded by `timeout_s`; any database
    error becomes AuditUnavailable, which the gateway turns into a denial."""
    config: AuditConfig
    _pool: Any = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def _get_pool(self) -> Any:
        import asyncpg                          # imported here so tests without a database never need it
        async with self._lock:
            if self._pool is None:
                c = self.config
                common = dict(min_size=0, max_size=4, timeout=c.timeout_s, command_timeout=c.timeout_s)
                if c.dsn:
                    # No prepared-statement cache: hosted poolers (Neon's PgBouncer) may hand each statement
                    # to a different server connection.
                    self._pool = await asyncpg.create_pool(dsn=c.dsn, statement_cache_size=0, **common)
                else:
                    self._pool = await asyncpg.create_pool(host=c.host, port=c.port, user=c.user,
                                                           password=c.password, database=c.database, **common)
        return self._pool

    async def _run(self, method: str, query: str, *params: Any) -> Any:
        try:
            pool = await asyncio.wait_for(self._get_pool(), self.config.timeout_s + 1)
            async with pool.acquire(timeout=self.config.timeout_s) as conn:
                return await getattr(conn, method)(query, *params)
        except Exception as exc:  # noqa: BLE001  asyncpg, OS and timeout errors all mean "not recorded"
            raise AuditUnavailable(type(exc).__name__) from None

    async def write(self, record: Mapping[str, Any]) -> None:
        row = _row(record)
        row["args"] = json.dumps(row["args"], sort_keys=True)
        await self._run("execute", INSERT_AUDIT, *(row[c] for c in AUDIT_COLUMNS))

    async def approval_requested(self, row: Mapping[str, Any]) -> None:
        await self._run("execute", INSERT_APPROVAL, row["request_id"], row["client_id"], row["tool"],
                        row.get("account_id"), row.get("subject"), row.get("trace_id"))

    async def approval_decided(self, request_id: str, status: str, approver: str | None) -> None:
        if status not in APPROVAL_STATUSES:
            raise AuditUnavailable("invalid approval status")
        # Exactly one pending row must change. Zero means the request was already closed (for example as
        # an orphan by another gateway process): a decision that is not recorded does not count.
        if await self._run("execute", UPDATE_APPROVAL, request_id, status, approver) != "UPDATE 1":
            raise AuditUnavailable("the approval was no longer pending")

    async def report(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_REPORT_ROWS))
        total = await self._run("fetchval", "SELECT count(*) FROM audit_log")
        outcomes = await self._run("fetch", "SELECT outcome, count(*) AS n FROM audit_log GROUP BY outcome ORDER BY outcome")
        reasons = await self._run("fetch", "SELECT reason, count(*) AS n FROM audit_log WHERE reason IS NOT NULL "
                                           "GROUP BY reason ORDER BY reason")
        recent = await self._run("fetch", SELECT_RECENT, limit)
        approvals = await self._run("fetch", SELECT_APPROVALS, limit)
        return {"total": total, "by_outcome": {r["outcome"]: r["n"] for r in outcomes},
                "by_reason": {r["reason"]: r["n"] for r in reasons},
                "recent": [_jsonable(r) for r in recent], "approvals": [_jsonable(r) for r in approvals]}

    async def close_orphaned_approvals(self) -> list[str]:
        rows = await self._run("fetch", CLOSE_ORPHANED)
        return sorted(r["request_id"] for r in rows)

    async def environment(self) -> str | None:
        """The environment the database was stamped with (db/stamp.sql): main, test or local. None when
        it was never stamped; AuditUnavailable when the database cannot be read."""
        try:
            name = await self._run("fetchval", SELECT_ENVIRONMENT)
        except AuditUnavailable as exc:
            if "UndefinedTable" in str(exc):
                return None
            raise
        return name if name in ENVIRONMENTS else None

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
