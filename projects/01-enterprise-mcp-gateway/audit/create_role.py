"""Create (or rotate) the least-privilege `gateway_audit` role in a hosted audit database such as Neon,
and point DATABASE_URL in .env at it.

    make audit-role P=01          # from the repo root

What it does, without ever printing a password or a URL:
  1. Generates a random password (stdlib `secrets`).
  2. Connects with the owner URL (DATABASE_ADMIN_URL, else DATABASE_URL_UNPOOLED) inside the one-off
     `audit-init` container and runs SQL: CREATE ROLE gateway_audit (or ALTER ROLE ... PASSWORD when it
     exists), removes any neon_superuser membership, then applies audit/schema.sql, whose grants give the
     role INSERT and SELECT on the audit tables plus UPDATE of the three decision columns of `approvals`
     (the same rights as the local role). The password reaches psql through an environment variable,
     never a command line.
  3. Rewrites the DATABASE_URL line in .env: same host, database and options as before, with the user and
     password of gateway_audit. Every other line of .env is left as it was.

The role is created with SQL on purpose: on Neon, roles created in the console, CLI or API become members
of neon_superuser; a role created with SQL does not.
"""
from __future__ import annotations

import os
import pathlib
import secrets
import subprocess
import sys
from urllib.parse import quote, urlsplit, urlunsplit

PROJECT = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT / ".env"
ROLE = "gateway_audit"

SQL = f"""
\\set ON_ERROR_STOP 1
\\getenv pw NEW_ROLE_PASSWORD
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}')
            THEN format('ALTER ROLE {ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'pw')
            ELSE format('CREATE ROLE {ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'pw')
       END \\gexec
SELECT format('REVOKE %I FROM {ROLE}', r.rolname)
  FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member
 WHERE u.rolname = '{ROLE}' \\gexec
\\i /audit/schema.sql
SELECT 'role ready: ' || rolname || ', superuser=' || rolsuper || ', member of '
       || coalesce((SELECT string_agg(r.rolname, ',') FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
                    WHERE m.member = u.oid), 'nothing')
  FROM pg_roles u WHERE rolname = '{ROLE}';
"""


def read_env_value(lines: list[str], key: str) -> str:
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def main() -> int:
    if not ENV_FILE.is_file():
        print("no .env: run `make up P=01` first", file=sys.stderr)
        return 2
    lines = ENV_FILE.read_text().splitlines()
    current = read_env_value(lines, "DATABASE_URL")
    if not current:
        print("DATABASE_URL is empty: this command is only for a hosted audit database (see "
              "docs/live-sandbox-setup.md, section 6)", file=sys.stderr)
        return 2
    if not (read_env_value(lines, "DATABASE_ADMIN_URL") or read_env_value(lines, "DATABASE_URL_UNPOOLED")):
        print("set DATABASE_ADMIN_URL (an owner role's URL) in .env first", file=sys.stderr)
        return 2
    parts = urlsplit(current)
    if not parts.hostname or "sslmode=" not in parts.query:
        print("DATABASE_URL must be a full postgresql:// URL with sslmode=require", file=sys.stderr)
        return 2

    password = secrets.token_urlsafe(32)               # 256 bits; meets Neon's password strength rule
    compose = ["docker", "compose", "-f", str(PROJECT / "docker-compose.yml"), "--profile", "admin", "run",
               "--rm", "--no-deps", "-T", "-e", "NEW_ROLE_PASSWORD", "--entrypoint", "sh", "audit-init", "-c",
               'psql "${DATABASE_ADMIN_URL:-$DATABASE_URL_UNPOOLED}" --quiet -f -']
    done = subprocess.run(compose, input=SQL, text=True, capture_output=True,
                          env={**os.environ, "NEW_ROLE_PASSWORD": password})
    if done.returncode != 0:
        # psql's own messages never contain the password (it is only in the environment), but they can
        # name the database host: redact every host from .env's URLs before printing.
        message = done.stderr.strip()[-2000:] or "creating the role failed"
        for key in ("DATABASE_ADMIN_URL", "DATABASE_URL_UNPOOLED", "DATABASE_URL"):
            host = urlsplit(read_env_value(lines, key)).hostname
            if host:
                message = message.replace(host, "<database host>")
        print(message, file=sys.stderr)
        return 1
    print("\n".join(l for l in done.stdout.splitlines() if "role ready" in l).strip())

    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    new_url = urlunsplit((parts.scheme, f"{ROLE}:{quote(password, safe='')}@{host}", parts.path, parts.query, ""))
    replaced = False
    out = []
    for line in lines:
        name, sep, _ = line.partition("=")
        if sep and name.strip() == "DATABASE_URL" and not replaced:
            out.append(f'DATABASE_URL="{new_url}"')
            replaced = True
        else:
            out.append(line)
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)
    print(f"DATABASE_URL in .env now connects as {ROLE} (same host and options). Run `make up P=01`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
