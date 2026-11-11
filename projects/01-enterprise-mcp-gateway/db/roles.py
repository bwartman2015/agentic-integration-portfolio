"""Create (or rotate) the two least-privilege roles in a hosted database such as Neon, and point the
connection strings in .env at them.

    make db-roles P=01          # from the repo root, after `make db-init P=01`

Roles:
  gateway_audit   the MCP gateway's audit log (DATABASE_URL): INSERT and SELECT on the audit tables,
                  UPDATE of the three decision columns of `approvals`
  copilot_memory  the CS copilot's long-term memory (MEMORY_DATABASE_URL): INSERT and SELECT on
                  memory.items only

What it does, without ever printing a password or a URL:
  1. Generates a random password per role (stdlib `secrets`).
  2. Connects with the owner URL (DATABASE_ADMIN_URL, else DATABASE_URL_UNPOOLED) inside the one-off
     `db-init` container and runs SQL: CREATE ROLE (or ALTER ROLE ... PASSWORD when it exists), removes
     any role memberships (such as neon_superuser), then applies db/audit.sql and db/memory.sql, whose
     grants are the only rights the roles get. Passwords reach psql through environment variables,
     never a command line.
  3. Rewrites DATABASE_URL and MEMORY_DATABASE_URL in .env: the host, database and options of the
     current DATABASE_URL, with each role's user and password. Every other line of .env is kept.

The roles are created with SQL on purpose: on Neon, roles created in the console, CLI or API become
members of neon_superuser; a role created with SQL does not. Running this again rotates both passwords
(restart with `make up P=01` afterwards).
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
ROLES = {"gateway_audit": ("DATABASE_URL", "NEW_GATEWAY_AUDIT_PASSWORD"),
         "copilot_memory": ("MEMORY_DATABASE_URL", "NEW_COPILOT_MEMORY_PASSWORD")}


def _role_sql(role: str, password_var: str) -> str:
    # A rotation changes only LOGIN and the password: a non-superuser owner (Neon's) may not even name
    # SUPERUSER or BYPASSRLS in ALTER ROLE. The attributes are checked after both roles exist (SQL below).
    return f"""
\\getenv pw_{role} {password_var}
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')
            THEN format('ALTER ROLE {role} WITH LOGIN PASSWORD %L', :'pw_{role}')
            ELSE format('CREATE ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
                        :'pw_{role}')
       END \\gexec
SELECT format('REVOKE %I FROM {role}', r.rolname)
  FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member
 WHERE u.rolname = '{role}' \\gexec
"""


SQL = ("\\set ON_ERROR_STOP 1\n" + "".join(_role_sql(r, v) for r, (_, v) in ROLES.items()) + """
DO $check$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('gateway_audit', 'copilot_memory')
             AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR rolreplication)) THEN
    RAISE EXCEPTION 'gateway_audit or copilot_memory has an elevated attribute; fix it as the database owner';
  END IF;
END $check$;
\\i /db/audit.sql
\\i /db/memory.sql
SELECT 'role ready: ' || u.rolname || ', superuser=' || u.rolsuper || ', member of '
       || coalesce((SELECT string_agg(r.rolname, ',') FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
                    WHERE m.member = u.oid), 'nothing')
  FROM pg_roles u WHERE u.rolname IN ('gateway_audit', 'copilot_memory') ORDER BY u.rolname;
""")


def read_env_value(lines: list[str], key: str) -> str:
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def write_env(lines: list[str], updates: dict[str, str]) -> list[str]:
    """Replace each key's line in place, append keys that are missing; keep everything else."""
    out, done = [], set()
    for line in lines:
        name, sep, _ = line.partition("=")
        key = name.strip() if sep else ""
        if key in updates and key not in done:
            out.append(f'{key}="{updates[key]}"')
            done.add(key)
        else:
            out.append(line)
    out += [f'{key}="{value}"' for key, value in updates.items() if key not in done]
    return out


def main() -> int:
    if not ENV_FILE.is_file():
        print("no .env: run `make up P=01` first", file=sys.stderr)
        return 2
    lines = ENV_FILE.read_text().splitlines()
    current = read_env_value(lines, "DATABASE_URL")
    if not current:
        print("DATABASE_URL is empty: this command is only for a hosted database (see "
              "docs/live-sandbox-setup.md, section 6)", file=sys.stderr)
        return 2
    if not (read_env_value(lines, "DATABASE_ADMIN_URL") or read_env_value(lines, "DATABASE_URL_UNPOOLED")):
        print("set DATABASE_ADMIN_URL (an owner role's URL) in .env first", file=sys.stderr)
        return 2
    parts = urlsplit(current)
    if not parts.hostname or "sslmode=" not in parts.query:
        print("DATABASE_URL must be a full postgresql:// URL with sslmode=require", file=sys.stderr)
        return 2

    passwords = {role: secrets.token_urlsafe(32) for role in ROLES}   # 256 bits each
    env = {**os.environ, **{var: passwords[role] for role, (_, var) in ROLES.items()}}
    compose = ["docker", "compose", "-f", str(PROJECT / "docker-compose.yml"), "--profile", "admin", "run",
               "--rm", "--no-deps", "-T", *[a for _, var in ROLES.values() for a in ("-e", var)],
               "--entrypoint", "sh", "db-init", "-c", 'psql "${DATABASE_ADMIN_URL:-$DATABASE_URL_UNPOOLED}" --quiet -f -']
    done = subprocess.run(compose, input=SQL, text=True, capture_output=True, env=env)
    if done.returncode != 0:
        # psql's messages never contain a password (passwords are only in the environment), but they
        # can name the database host: redact every host from .env's URLs before printing.
        message = done.stderr.strip()[-2000:] or "creating the roles failed"
        for key in ("DATABASE_ADMIN_URL", "DATABASE_URL_UNPOOLED", "DATABASE_URL", "MEMORY_DATABASE_URL"):
            host = urlsplit(read_env_value(lines, key)).hostname
            if host:
                message = message.replace(host, "<database host>")
        print(message, file=sys.stderr)
        return 1
    print("\n".join(line for line in done.stdout.splitlines() if "role ready" in line).strip())

    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    updates = {key: urlunsplit((parts.scheme, f"{role}:{quote(passwords[role], safe='')}@{host}",
                                parts.path, parts.query, ""))
               for role, (key, _) in ROLES.items()}
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(write_env(lines, updates)) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)
    print("DATABASE_URL (gateway_audit) and MEMORY_DATABASE_URL (copilot_memory) in .env are updated "
          "(same host and options). Run `make up P=01`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
