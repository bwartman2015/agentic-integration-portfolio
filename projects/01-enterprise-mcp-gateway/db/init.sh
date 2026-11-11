#!/bin/sh
# Local database for the Ridgeline gateway and copilot (Milestones 5-6). Runs once, when the Postgres
# data volume is first created (docker-entrypoint-initdb.d): creates the two least-privilege roles,
# then the schemas from db/audit.sql and db/memory.sql (the same files `make db-init` applies to a
# hosted database such as Neon).
#
#   gateway_audit   the MCP gateway: INSERT and SELECT on the audit tables (plus the decision columns
#                   of approvals); triggers in audit.sql refuse changes and deletes for every role
#   copilot_memory  the CS copilot: INSERT and SELECT on memory.items only
# The superuser password stays with the database container.
set -eu
: "${AUDIT_DB_PASSWORD:?AUDIT_DB_PASSWORD must be set}"
: "${COPILOT_MEMORY_DB_PASSWORD:?COPILOT_MEMORY_DB_PASSWORD must be set}"

# Passwords are read inside psql (\getenv), so they never appear on a command line or in this file.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv gw_pw AUDIT_DB_PASSWORD
\getenv mem_pw COPILOT_MEMORY_DB_PASSWORD
CREATE ROLE gateway_audit LOGIN PASSWORD :'gw_pw';
CREATE ROLE copilot_memory LOGIN PASSWORD :'mem_pw';
\i /db/audit.sql
\i /db/memory.sql
SQL
