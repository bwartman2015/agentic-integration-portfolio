"""Create (or rotate) the two least-privilege roles in a hosted database such as Neon, and point the
connection strings in .env at them.

    make db-roles P=01              # from the repo root, after `make db-init P=01`
    make db-roles P=01 TARGET=test  # the Neon `test` branch (after `make db-init P=01 TARGET=test`)

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
     TARGET=test works on the `test` branch instead (owner URL DATABASE_ADMIN_URL_TEST) and writes
     DATABASE_URL_TEST and MEMORY_DATABASE_URL_TEST (sslmode=verify-full); main's URLs are not touched.
     Roles are per branch, so the test branch's passwords differ from main's.

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
TARGETS = ("main", "test")


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
DO $$ BEGIN
  IF to_regclass('public.ridgeline_environment') IS NOT NULL THEN
    GRANT SELECT ON public.ridgeline_environment TO gateway_audit, copilot_memory;
  END IF;
END $$;
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


MAIN_URL_KEYS = ("DATABASE_ADMIN_URL", "DATABASE_URL_UNPOOLED", "DATABASE_URL", "MEMORY_DATABASE_URL")


def endpoint(url: str) -> str:
    """A database endpoint, comparable across Neon's pooled and direct host names (`-pooler`)."""
    host = (urlsplit(url).hostname or "").lower()
    first, dot, rest = host.partition(".")
    return first.removesuffix("-pooler") + dot + rest


def target_plan(lines: list[str], target: str) -> tuple[str, str, dict[str, str]] | str:
    """(base URL whose host/database/options the role URLs reuse, query string, {role: .env key}) for
    the target, or an error message."""
    if target not in TARGETS:
        return f"TARGET must be one of {', '.join(TARGETS)}"
    if target == "test":
        admin = read_env_value(lines, "DATABASE_ADMIN_URL_TEST")
        if not admin:
            return "set DATABASE_ADMIN_URL_TEST (the test branch owner's URL) in .env first"
        # Neon endpoints are per branch: a "test" URL that reaches a main endpoint would stamp main as
        # `test` and rotate its roles, which would let the wire tests write to main. Refuse it.
        mains = {endpoint(v) for v in (read_env_value(lines, k) for k in MAIN_URL_KEYS) if v}
        if endpoint(admin) in mains:
            return "DATABASE_ADMIN_URL_TEST points at the same database as the main URLs; refusing"
        return admin, "sslmode=verify-full", {role: key + "_TEST" for role, (key, _) in ROLES.items()}
    current = read_env_value(lines, "DATABASE_URL")
    if not current:
        return ("DATABASE_URL is empty: this command is only for a hosted database (see "
                "docs/live-sandbox-setup.md, section 6)")
    if not (read_env_value(lines, "DATABASE_ADMIN_URL") or read_env_value(lines, "DATABASE_URL_UNPOOLED")):
        return "set DATABASE_ADMIN_URL (an owner role's URL) in .env first"
    return current, urlsplit(current).query, {role: key for role, (key, _) in ROLES.items()}


def main() -> int:
    if not ENV_FILE.is_file():
        print("no .env: run `make up P=01` first", file=sys.stderr)
        return 2
    lines = ENV_FILE.read_text().splitlines()
    target = (os.environ.get("DB_TARGET") or "main").strip()
    plan = target_plan(lines, target)
    if isinstance(plan, str):
        print(plan, file=sys.stderr)
        return 2
    base, query, keys = plan
    parts = urlsplit(base)
    if not parts.hostname or "sslmode=" not in parts.query:
        print("the database URL must be a full postgresql:// URL with sslmode=require", file=sys.stderr)
        return 2

    passwords = {role: secrets.token_urlsafe(32) for role in ROLES}   # 256 bits each
    env = {**os.environ, **{var: passwords[role] for role, (_, var) in ROLES.items()}}
    admin = ('"$DATABASE_ADMIN_URL_TEST"' if target == "test"
             else '"${DATABASE_ADMIN_URL:-$DATABASE_URL_UNPOOLED}"')
    compose = ["docker", "compose", "-f", str(PROJECT / "docker-compose.yml"), "--profile", "admin", "run",
               "--rm", "--no-deps", "-T", *[a for _, var in ROLES.values() for a in ("-e", var)],
               "--entrypoint", "sh", "db-init", "-c", f"psql {admin} --quiet -f -"]
    done = subprocess.run(compose, input=SQL, text=True, capture_output=True, env=env)
    if done.returncode != 0:
        # psql's messages never contain a password (passwords are only in the environment), but they
        # can name the database host: redact every host from .env's URLs before printing.
        message = done.stderr.strip()[-2000:] or "creating the roles failed"
        for key in ("DATABASE_ADMIN_URL", "DATABASE_URL_UNPOOLED", "DATABASE_URL", "MEMORY_DATABASE_URL",
                    "DATABASE_ADMIN_URL_TEST", "DATABASE_URL_TEST", "MEMORY_DATABASE_URL_TEST"):
            host = urlsplit(read_env_value(lines, key)).hostname
            if host:
                message = message.replace(host, "<database host>")
        print(message, file=sys.stderr)
        return 1
    print("\n".join(line for line in done.stdout.splitlines() if "role ready" in line).strip())

    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    updates = {keys[role]: urlunsplit((parts.scheme, f"{role}:{quote(passwords[role], safe='')}@{host}",
                                      parts.path, query, ""))
               for role in ROLES}
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(write_env(lines, updates)) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)
    print(f"{keys['gateway_audit']} (gateway_audit) and {keys['copilot_memory']} (copilot_memory) in .env "
          f"are updated ({target} branch). Run `make up P=01{' DB=test' if target == 'test' else ''}`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
