-- Audit schema for the Ridgeline MCP gateway (Milestone 5). Idempotent: safe to run again.
-- Local Postgres: db/init.sh runs it when the data volume is first created.
-- Neon (or any hosted Postgres): `make db-init P=01` runs it with DATABASE_ADMIN_URL (and `make db-roles
-- P=01` again, after creating the roles, so their grants apply).
--
-- Append-only, twice over:
--   1. Triggers refuse UPDATE, DELETE and TRUNCATE on audit_log, and allow on approvals only the one
--      change a decision makes (pending -> approved/rejected/timed_out/unavailable). They apply to every
--      role, the table owner included, so the rule holds even when the gateway connects as the owner.
--   2. If the role gateway_audit exists, it gets only INSERT and SELECT (plus the decision columns of
--      approvals). Point the gateway at that role for the strongest setup.

CREATE TABLE IF NOT EXISTS audit_log (
    id              bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    client_id       text,                       -- verified token client; NULL only for unauthenticated in-process calls
    tool            text NOT NULL,              -- "<unknown>" for a tool the gateway does not have
    outcome         text NOT NULL,              -- ok | denied | not_found | invalid_arguments | upstream_error | unknown_tool | error
    reason          text,                       -- reason code of a denial (policy/data.json)
    scopes          text[] NOT NULL DEFAULT '{}',
    regions         text[] NOT NULL DEFAULT '{}',
    args            jsonb NOT NULL DEFAULT '{}',-- argument values with PII masked and long text cut
    latency_ms      real,
    backend         text,
    upstream_status integer,
    request_id      text,                       -- JSON-RPC request id
    trace_id        text,                       -- traceparent / x-request-id sent by the agent, if any
    approval_id     text,                       -- approvals.request_id for a write tool
    pii_masked      integer NOT NULL DEFAULT 0  -- values masked in the result returned to the caller
);
CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log (at DESC);
CREATE INDEX IF NOT EXISTS audit_log_client ON audit_log (client_id, at DESC);

CREATE TABLE IF NOT EXISTS approvals (
    request_id   text PRIMARY KEY,
    requested_at timestamptz NOT NULL DEFAULT now(),
    client_id    text,
    tool         text NOT NULL,
    account_id   text,
    subject      text,
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected', 'timed_out', 'unavailable')),
    approver     text,                          -- the name the human gave on the approval form
    decided_at   timestamptz
);
-- Milestone 8: the trace id of the agent run that asked (x-request-id), so an operator console can match a
-- waiting approval to its run exactly. Written once with the request, like the other request columns.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS trace_id text;

CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP;
END $$;

CREATE OR REPLACE FUNCTION approvals_decide_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'approvals rows cannot be removed (% refused)', TG_OP;
    END IF;
    IF OLD.status <> 'pending' OR NEW.status = 'pending'
       OR (NEW.request_id, NEW.requested_at, NEW.client_id, NEW.tool, NEW.account_id, NEW.subject)
          IS DISTINCT FROM (OLD.request_id, OLD.requested_at, OLD.client_id, OLD.tool, OLD.account_id, OLD.subject) THEN
        RAISE EXCEPTION 'an approval is decided once, and only its decision columns change';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();
DROP TRIGGER IF EXISTS approvals_decide_once ON approvals;
CREATE TRIGGER approvals_decide_once BEFORE UPDATE OR DELETE ON approvals
    FOR EACH ROW EXECUTE FUNCTION approvals_decide_once();
DROP TRIGGER IF EXISTS approvals_no_truncate ON approvals;
CREATE TRIGGER approvals_no_truncate BEFORE TRUNCATE ON approvals
    FOR EACH STATEMENT EXECUTE FUNCTION approvals_decide_once();

REVOKE ALL ON audit_log, approvals FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_audit') THEN
        GRANT INSERT, SELECT ON audit_log TO gateway_audit;
        GRANT USAGE ON SEQUENCE audit_log_id_seq TO gateway_audit;
        GRANT INSERT (request_id, client_id, tool, account_id, subject, trace_id), SELECT ON approvals TO gateway_audit;
        GRANT UPDATE (status, approver, decided_at) ON approvals TO gateway_audit;
    END IF;
END $$;
