#!/bin/sh
# Local audit database for the Ridgeline MCP gateway (Milestone 5). Runs once, when the Postgres data
# volume is first created (docker-entrypoint-initdb.d): creates the gateway's role, then the schema
# (audit/schema.sql, the same file `make audit-init` applies to a hosted database such as Neon).
#
# The gateway connects as `gateway_audit`, which may INSERT and SELECT audit rows but not UPDATE or
# DELETE them; triggers in schema.sql refuse those changes for every role. The superuser password
# stays with the database container.
set -eu
: "${AUDIT_DB_PASSWORD:?AUDIT_DB_PASSWORD must be set}"

# The password is read inside psql (\getenv), so it never appears on a command line or in this file.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv pw AUDIT_DB_PASSWORD
CREATE ROLE gateway_audit LOGIN PASSWORD :'pw';
\i /audit/schema.sql
SQL
