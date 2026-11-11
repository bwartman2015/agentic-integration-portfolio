-- Long-term memory for the CS copilot (Milestone 6). Idempotent: safe to run again.
-- Local Postgres: db/init.sh runs it when the data volume is first created.
-- Neon (or any hosted Postgres): `make db-init P=01` runs it with DATABASE_ADMIN_URL.
--
-- One table of embedded memories in its own schema. The copilot's role (copilot_memory) may add and
-- read rows here and nothing else: no audit table, no other schema, no UPDATE or DELETE.
-- Recall never mixes callers or embedding models: every query filters on `audience` (the caller's
-- scopes and regions) and `model` in SQL.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS memory;

CREATE TABLE IF NOT EXISTS memory.items (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    audience    text NOT NULL,              -- who may recall it: the caller's scopes and regions
    kind        text NOT NULL CHECK (kind IN ('episode', 'alias', 'fact', 'pattern')),
    text        text NOT NULL,              -- PII-masked before it is written
    meta        jsonb NOT NULL DEFAULT '{}',
    model       text NOT NULL,              -- embedding model; vectors of different models never meet
    embedding   vector(768) NOT NULL
);
CREATE INDEX IF NOT EXISTS items_embedding ON memory.items USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS items_audience_model ON memory.items (audience, model, created_at DESC);

REVOKE ALL ON SCHEMA memory FROM PUBLIC;
-- No temporary tables either: PUBLIC's default TEMP right on the database is withdrawn (the owner keeps it).
DO $$ BEGIN
  EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END $$;
REVOKE ALL ON memory.items FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'copilot_memory') THEN
        GRANT USAGE ON SCHEMA memory TO copilot_memory;
        GRANT SELECT, INSERT ON memory.items TO copilot_memory;
        GRANT USAGE ON SEQUENCE memory.items_id_seq TO copilot_memory;
    END IF;
END $$;
