-- Stamp a database with the environment it serves (Milestone 7): main (demo runs), test (automated
-- tests) or local. The gateway reads it at startup and reports it in /healthz; the wire tests refuse to
-- run against `main`. The stamp lives in the database itself, so a wrong URL in .env cannot fool the
-- check. A Neon branch starts as a copy of its parent (and its stamp) until it is stamped itself.
-- Usage: psql -v environment=test -f db/stamp.sql   (`make db-init` and db/init.sh run it)
\set ON_ERROR_STOP 1
CREATE TABLE IF NOT EXISTS public.ridgeline_environment (
    one  boolean PRIMARY KEY DEFAULT true CHECK (one),       -- exactly one row
    name text NOT NULL CHECK (name IN ('main', 'test', 'local'))
);
INSERT INTO public.ridgeline_environment (one, name) VALUES (true, :'environment')
    ON CONFLICT (one) DO UPDATE SET name = EXCLUDED.name;
REVOKE ALL ON public.ridgeline_environment FROM PUBLIC;
-- The gateway and the copilot may read the stamp, never change it.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_audit') THEN
    GRANT SELECT ON public.ridgeline_environment TO gateway_audit;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'copilot_memory') THEN
    GRANT SELECT ON public.ridgeline_environment TO copilot_memory;
  END IF;
END $$;
