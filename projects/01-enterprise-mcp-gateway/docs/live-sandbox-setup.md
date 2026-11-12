# Live sandbox setup: Salesforce, SAP and Neon

The gateway works offline by default. Milestone 2's live check needs two free accounts:

| System | Gateway tool | Backend setting | What you need |
|---|---|---|---|
| Salesforce Developer Edition | `search_accounts` | `CRM_BACKEND=salesforce` | An External Client App using the OAuth 2.0 **client credentials** flow |
| SAP Business Accelerator Hub sandbox | `get_sales_order` | `ERP_BACKEND=sap_sandbox` | Your personal API key |

The mock carrier and AR APIs need nothing from you. `make up P=01` generates their shared secrets. Keeping the audit log in Neon instead of the local Postgres is optional: see [section 6](#6-optional-keep-the-audit-log-in-neon-milestone-5).

> **Secrets go only in `projects/01-enterprise-mcp-gateway/.env`.** That file is git-ignored and created with owner-only permissions. Never paste a secret into a chat, an issue, a commit or a screenshot. If one leaks, rotate it: in Salesforce, regenerate the consumer secret; in SAP, sign out and back in to get a new key.

---

## 1. Salesforce Developer Edition

### 1a. Create the org (about 5 minutes)
1. Go to `https://developer.salesforce.com/signup` and sign up. Use a username shaped like an email that nobody else has used (for example `you.ridgeline@example.com`; it doesn't have to be a real inbox). The **Email** field must be real, because the activation link goes there.
2. Activate the account from the email and set a password.
3. In the org, go to **Setup** (gear icon) → search **My Domain**. Copy the **Current My Domain URL**. It looks like `https://yourname-dev-ed.develop.my.salesforce.com`. This is your `SF_LOGIN_URL`.
   - The client credentials flow **only works on the My Domain URL**, not on `login.salesforce.com`.

### 1b. Create the External Client App (about 10 minutes)
1. **Setup** → search **External Client App Manager** → **New External Client App**.
2. **Basic Information**:
   - Name: `Ridgeline MCP Gateway`
   - Contact email: your email
   - Distribution state: **Local**
3. **API (Enable OAuth Settings)** → tick **Enable OAuth**:
   - Callback URL: `https://login.salesforce.com/services/oauth2/success`. The client credentials flow never uses it, but the form requires one.
   - OAuth scopes: add **Manage user data via APIs (api)**. Nothing else.
   - Flow Enablement: tick **Enable Client Credentials Flow** and acknowledge the warning.
   - Security: keep **Require secret for Web Server Flow** and **Require secret for Refresh Token Flow** ticked. Leave PKCE as it is.
4. **Create**.
5. Open the app → **Policies** tab → **Edit** → **OAuth Policies**:
   - Tick **Enable Client Credentials Flow**.
   - **Run As (Username)**: the user every API call runs as, with that user's permissions.
     - **Quick start:** pick your own System Administrator user. The gateway's CRM connector can only send `GET` requests, enforced in code. Only the operator seed tool can write, and only `POST`/`PATCH` on its own synthetic records.
     - **Recommended:** a dedicated integration user with minimum access. In **Setup** → **Users** → **New User**, use the **Salesforce Integration** license (Developer Edition includes it). Assign it the **Salesforce API Integration** permission set license; object permissions for this license can only come from permission sets that use it. Then give it a permission set with *API Enabled* and *Read/Create/Edit* on Account and Contact. Create/Edit is needed only for `make seed-salesforce`; remove it afterwards for read-only. Then pick that user as Run As.
   - **Save**.
6. **Settings** tab → **OAuth Settings** → **Consumer Key and Secret**. Salesforce emails you a verification code; enter it.
   - Copy the **Consumer Key**. This is `SF_CLIENT_ID`.
   - Copy the **Consumer Secret**. This is `SF_CLIENT_SECRET`.
7. Wait up to **10 minutes**. New External Client Apps take a while to become active, and until then the token request fails with `invalid_client`.

Optional check from your terminal: a token request that returns an `access_token` means the app is ready. Don't paste the output anywhere.
```bash
# after step 3, from projects/01-enterprise-mcp-gateway (loads .env into this shell only)
set -a; . ./.env; set +a
curl -s -X POST "$SF_LOGIN_URL/services/oauth2/token" -d grant_type=client_credentials \
  --data-urlencode "client_id=$SF_CLIENT_ID" --data-urlencode "client_secret=$SF_CLIENT_SECRET" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if 'access_token' in d else d)"
```

## 2. SAP Business Accelerator Hub API key (about 3 minutes)
1. Go to `https://api.sap.com` and **Log On**. Register for a free SAP account if you don't have one.
2. Search for **Sales Order (A2X)**. This is the OData v2 service `API_SALES_ORDER_SRV` under *SAP S/4HANA Cloud Public Edition*. Open it.
3. Click **Show API Key** (top right of the API page) → **Copy Key to Clipboard**. This is `SAP_API_KEY`.
   - The key belongs to your SAP account and works for every sandbox API on the hub.
   - The sandbox is read-only SAP demo data and is rate limited. The gateway only reads.

Leave `SAP_BASE_URL` at its default: `https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_SALES_ORDER_SRV`.

## 3. Put the values in `.env`
`make up P=01` already created `.env` from `.env.example`, with random mock-API secrets. Don't copy `.env.example` over it. Edit the existing file instead:
```bash
cd ~/agentic-integration-portfolio/projects/01-enterprise-mcp-gateway
open -e .env        # TextEdit; save as plain text
```
Change these lines (no quotes, no spaces around `=`):
```
CRM_BACKEND=salesforce
SF_LOGIN_URL=https://yourname-dev-ed.develop.my.salesforce.com
SF_CLIENT_ID=<Consumer Key>
SF_CLIENT_SECRET=<Consumer Secret>

ERP_BACKEND=sap_sandbox
SAP_API_KEY=<SAP API key>
```
If `.env` doesn't exist yet, run `make up P=01` once first. Don't create it with `cp`: the `__GENERATE__` placeholders would stay as literal text, and the mock APIs refuse to start.

## 4. Seed Salesforce, then run the live check
From the repo root:
```bash
make up P=01                                   # restarts the gateway with the new settings
make seed-salesforce P=01                      # dry run: lists the accounts/contacts it would create
make seed-salesforce P=01 ARGS=--apply         # writes 8 synthetic Ridgeline accounts + contacts
make live-check P=01                           # all 4 tools through MCP, with the backend for each
```
Expected `live-check` output once everything is live. The backend column shows what `.env` configures; the gateway's log line for each call records what actually answered (`docker compose -f projects/01-enterprise-mcp-gateway/docker-compose.yml logs gateway`):
```
tool               backend      result  summary   (backend = configured in .env)
search_accounts    salesforce   ok      1 match(es): Summit Contractors LLC (ACCT-1002)
get_sales_order    sap_sandbox  ok      SO-000001 status=... total=... USD
track_shipment     carrier_api  ok      1 shipment(s), first status=EXCEPTION
get_open_invoices  ar_api       ok      1 open invoice(s), total_open=8370.0
all tools returned data
```
Notes:
- **Alias search (`BBO`) uses SOSL.** Salesforce indexes new records for search after a few minutes. Name and account-number searches work right away.
- **The seed script is idempotent and never deletes.**
  - It matches accounts on `AccountNumber` (`ACCT-xxxx`) and refuses anything that isn't synthetic (emails must be `@example.com`).
  - It updates an existing account only if the seeder created it (its Description starts with `Ridgeline synthetic account (fictional)`). Any other account with the same number is skipped and reported.
  - Running it again creates nothing new.
- **SAP's order numbers map to `SO-` plus 6 digits** (`1` becomes `SO-000001`). `live-check` asks the sandbox for an existing number first. Ridgeline's demo scenario (SO-104233) exists only in the seed ERP, so the copilot demo keeps `ERP_BACKEND=seed`.
- **With live CRM/ERP, run the wire tests as `GATEWAY_LIVE_BACKENDS=1 make test P=01`.** That skips the two comparisons against seeded CRM/ERP data.

## 5. Identity: Keycloak (Milestone 3, nothing to set up)
`make up P=01` starts Keycloak and generates its secrets in `.env`: the admin password and the `cs-rep`/`cs-lead` client secrets. Existing values are never changed. Every gateway call now needs a token:
- **`make live-check P=01`** gets a `cs-rep` token itself.
- **MCP Inspector:** run `make token P=01 CLIENT=cs-rep` and paste the output as the Bearer token (Authentication → Bearer Token). It's valid for 5 minutes.
- **Keycloak admin console:** `http://localhost:8080/`, user `admin`, password = `KC_ADMIN_PASSWORD` in `.env`.

## 5b. Approvals: create the n8n owner account (Milestone 5, once)
The approval forms work without logging in, but n8n's editor (where the workflow and its runs are visible) is open to whoever claims it first. Claim it right after the first `make up P=01`:
1. Open http://localhost:5678. The page says **Set up owner account**.
2. Enter an email you'll remember (it stays in n8n's database on your machine and is never verified), your name, and a password with at least 8 characters, one number and one capital letter. Click **Next**.
3. If a box offers paid features for free, click **Skip**. The workflow **Ridgeline: approve a gateway write** is listed as published. Don't edit it in the UI: it is re-imported from `n8n/ridgeline-approval.json` at every start.

To try an approval end to end: run `make request-case P=01` in one terminal, `make approvals P=01` in another, open the link it prints, choose **Approve**, type your name and submit within `APPROVAL_TIMEOUT_S` (120 s). Don't restart the stack while a request is waiting: the request can no longer be completed (nothing is written, and the next start closes it as `unavailable`).

## 6. Optional: keep the audit log and the copilot's memory in Neon (Milestones 5-6)
By default both live in the local `db` container (Postgres 17 with pgvector; since Milestone 6 its volume is `ridgeline-p01_db-data`, so local audit history starts fresh, and the old `ridgeline-p01_audit-data` volume can be deleted with `docker volume rm`), and `make up` and the tests use it: the gateway's audit log in schema `public`, the copilot's long-term memory in schema `memory`. To keep them in [Neon](https://neon.com) (serverless Postgres, free plan) instead, set `DATABASE_URL` in `.env` and run the two commands below; they also set `MEMORY_DATABASE_URL`, which the copilot uses.

`neon.ts`, `package.json`, `package-lock.json` and `.gitignore` in `projects/01-enterprise-mcp-gateway` belong to this optional setup only. They are the Neon CLI's project config and the two packages `neon.ts` needs. The gateway, the tests and the local stack never use them, and you don't need Node.js unless you set up Neon.

1. Create a free Neon account and a project. Its `production` branch already has Postgres.
2. Install the Neon CLI and sign in. The sign-in finishes in your browser:
   ```bash
   npm i -g neon@latest
   neon login
   ```
3. Link this project. From `projects/01-enterprise-mcp-gateway`, using the IDs from the Neon console:
   ```bash
   neon init -y --no-agent-setup --org-id <org-id> --project-id <project-id> --branch production --services postgres
   ```
   This adds `DATABASE_URL` (pooled) and `DATABASE_URL_UNPOOLED` (direct) to `.env`, both as the database owner `neondb_owner`. Your other values are kept. You can also paste the connection strings from the Neon console (**Connect**) into `.env` yourself. They must contain `sslmode=require`.
4. Create the tables in Neon. This is one time, and safe to repeat:
   ```bash
   make db-init P=01    # applies db/audit.sql and db/memory.sql (adds the pgvector extension) as the owner,
                        # and stamps the database `main` (db/stamp.sql)
   ```
5. Give the gateway and the copilot their own least-privilege roles:
   ```bash
   make db-roles P=01
   ```
   This creates (or, run again, rotates) the roles `gateway_audit` and `copilot_memory` with random passwords, applies their grants, and rewrites `DATABASE_URL` (gateway) and `MEMORY_DATABASE_URL` (copilot) in `.env` to connect as them. It changes nothing else in `.env`, prints no password, and stops if either role has any elevated attribute or membership. `DATABASE_URL_UNPOOLED` stays the owner's URL, used only by `make db-init` and `make db-roles`.
   - **`gateway_audit`:** the same rights as the local role: INSERT and SELECT on `audit_log`, and on `approvals` INSERT of the request columns, SELECT, and UPDATE of only `status`, `approver` and `decided_at`. It cannot update or delete audit rows, truncate, drop or alter tables, create tables, turn off the triggers, or touch the `memory` schema.
   - **`copilot_memory`:** INSERT and SELECT on `memory.items`, nothing else: no UPDATE or DELETE of memories, no audit tables, no DDL.
   - **Why SQL, not the console:** the roles are created with SQL on purpose. Neon makes roles created in its console, CLI or API members of `neon_superuser`, which would defeat least privilege.
6. `make up P=01`. Check that `docker compose logs gateway` shows `"audit": {"store": "postgres", "source": "database_url", "host": "*.neon.tech"}`. `make audit P=01` now reads from Neon, and `make ask P=01` keeps the copilot's memory there.

**Keep Neon commands away from `DATABASE_URL`.** `neon env pull`, `neon deploy`, `neon link` and `neon checkout` write the owner's `DATABASE_URL` into `.env` by default, which would undo step 5. From now on:
```bash
neon env pull --file .env.neon     # a separate, git-ignored file; copy any value you need by hand
neon deploy --no-env-pull
neon checkout <branch> --no-env-pull
```
If one of them did overwrite `DATABASE_URL`, run `make db-roles P=01` again.

How it behaves:
- **TLS is enforced.** A remote `DATABASE_URL` without `sslmode=require` (or `verify-full`) stops the gateway at startup. `require` encrypts but does not check the server's certificate; **`sslmode=verify-full` is recommended** (change `require` to `verify-full` in `DATABASE_URL`). The gateway then checks Neon's certificate against the image's CA bundle. The URL is never logged; the startup line shows only the provider domain.
- **Append-only twice over.** The role's grants allow only adding and reading. Triggers in `db/audit.sql` also refuse UPDATE, DELETE and TRUNCATE on the audit log for every role, the owner included.
- **Fail closed.** If Neon is unreachable or its compute is waking up past `AUDIT_TIMEOUT_S`, reads are refused (`audit_unavailable`). Retry after a few seconds.
- **Restarts during an approval.** If the gateway restarts while a write is waiting for a human, that request can't be completed, and nothing is written. The next startup closes it as `unavailable`, and `make request-case` reports it in one line. Request again after the restart.
- **Switch back:** empty `DATABASE_URL` and `MEMORY_DATABASE_URL` in `.env`, then `make up P=01`. The local container keeps running either way, so switching needs no other change. Memories do not move between databases; each starts empty.

**Never commit the URL.** It holds the database password. It lives only in the git-ignored `.env` (and `.env.neon`, if you pull into one), and the local public-content scanner (`make safety`, not part of the repository) fails if a database URL with a password appears in a tracked file. If it leaks, run `make db-roles P=01` to rotate both roles' passwords, and reset the owner's password in the Neon console.

## 6b. A Neon `test` branch for automated tests (Milestone 7)
The wire tests write audit rows and approval records. They never run against the `main` branch, which keeps the demo runs. Every database is stamped with what it is (`main`, `test` or `local`, in the one-row table `ridgeline_environment`). The gateway reads the stamp at startup and reports it in `/healthz`, and the wire tests skip (or fail under `REQUIRE_GATEWAY=1`) unless it says `test` or `local`. The stamp lives in the database itself, so a wrong URL in `.env` cannot fool the check.

1. Create the branch from `production`, without printing credentials:
   ```bash
   neon branches create --name test --parent production --no-secrets
   ```
2. Put the branch owner's URL into `.env` as `DATABASE_ADMIN_URL_TEST` (`neon cs test` prints it, or copy it from the console; keep `sslmode=require`).
3. Stamp it and give it its own least-privilege roles (roles are per branch, so their passwords differ from `main`'s):
   ```bash
   make db-init P=01 TARGET=test     # schemas + the `test` stamp
   make db-roles P=01 TARGET=test    # writes DATABASE_URL_TEST and MEMORY_DATABASE_URL_TEST
   ```
4. Run the tests on it, then go back to `main` for demos:
   ```bash
   make up P=01 DB=test              # or DB=local for the local Postgres
   REQUIRE_GATEWAY=1 make test P=01
   make up P=01                      # demo runs: the database DATABASE_URL names (Neon main)
   ```
`curl -s localhost:8765/healthz` shows which one the stack is on. `main` was used for development before the test branch existed: its early audit rows are development history, kept because the log is append-only.

## 7. Real models (Milestone 7)
The project is free to run end to end. Beside the scripted model the tests use, the real model is a **local** one: Qwen2.5-1.5B-Instruct on the llama.cpp server (free, offline, slow on a laptop CPU). Download it once (1.1 GB, SHA-256 pinned):

```bash
make models P=01 MODEL=chat
make eval P=01 LLM=local       # the official real-model baseline; 1.5-2 hours on an older laptop (below)
make ask P=01 LLM=local Q="Is anything still open for Pinecrest Hardware?"
```
Evals never touch a database: they run on the in-process mock systems with in-memory memory. Add `ARGS=--update-baseline` to save a run as the model's baseline (`evals/baseline.local.json`).

**Speed.** The model runs on the CPU inside Docker's Linux VM. Check which vector instructions that VM gets: `docker run --rm alpine grep -m1 -o -w avx2 /proc/cpuinfo`. On some Intel Macs it reports nothing (the VM hides AVX2 even when the processor has it), and llama.cpp falls back to a much slower AVX-only path: about 7 prompt tokens and 2-3 generated tokens per second on a 2015 dual-core i5, so a full eval takes 1.5-2 hours. The `llm` service uses 2 threads (physical cores beat hyperthreads here) and two cache slots, so the planner's and the reasoner's prompt prefixes are not recomputed. With AVX2 (newer Macs, Linux hosts) expect several times faster.

**Optional, paid: a hosted model.** The copilot can also use Claude through the official SDK (`COPILOT_LLM_PROVIDER=anthropic`, or `LLM=claude` on `make eval` / `make ask` / `make a2a-up`). Nothing in the project needs it. To try it, set in `.env` your own `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_PRICE_IN_PER_MTOK` / `_OUT_PER_MTOK` (USD per million tokens, so every run prints its cost) and optionally `ANTHROPIC_EFFORT`. Another hosted model would be one more provider class in `cs_copilot/agent/reason.py`.

## 8. The copilot over A2A (Milestone 7)
```bash
make a2a-up P=01      # Agent Card at http://localhost:8701/.well-known/agent-card.json
make a2a-check P=01   # card, a refused request without a token, then a task as cs-rep
```
Callers authenticate with their own Keycloak token (client credentials), which the copilot passes through to the gateway: see `cs_copilot/a2a_server.py`. Run the check on a test stack (`DB=test`), since it makes real gateway calls.

## 9. The web console (Milestone 8)
```bash
make up P=01            # the stack (add DB=test to keep demo runs out of the main branch)
make console-up P=01    # builds if needed, starts the console and the local model, prints the sign-in link
```
Open the link it prints (http://localhost:8790/?token=...; keep it private, like the approval links). It signs this browser in for the session; the token itself is `CONSOLE_TOKEN` in `.env`, generated by `make up`. Pick a role and a model, ask, and follow the run. For the approval demo, ask as **cs-lead**: "Open a case for Summit Contractors about the late delivery of SO-104233 and confirm the new delivery window." When the page shows **Waiting for a human decision**, click **Open the approval form**, choose Approve, type a name and submit within the time shown. `make console-check P=01` runs one question through the console on a test stack.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `make up` fails and `docker compose logs gateway` shows `startup_failed ... SF_CLIENT_ID is required` | A live backend is selected but a value is missing or misspelled in `.env`. The gateway fails closed by design. |
| `startup_failed ... SF_LOGIN_URL must be https://<your-domain>.my.salesforce.com` | Use the My Domain URL from step 1a, starting with `https://`. |
| `Salesforce returned an error (HTTP 400)` | `invalid_client` or `invalid_grant`: the app isn't active yet (wait 10 minutes), the client credentials flow or the Run As user isn't set (step 1b.5), or the key or secret is wrong. |
| `Salesforce rejected the gateway's credentials.` | HTTP 401: the token was refused even after a refresh. Check that the External Client App is still enabled and the secret hasn't been rotated. |
| `Salesforce refused the request (not permitted, or an API limit was reached).` | HTTP 403. Three possible causes:<br>• the Run As user lacks **API Enabled** or access to Account/Contact;<br>• the org's daily API limit is used up (`REQUEST_LIMIT_EXCEEDED`);<br>• the API is disabled for the org. |
| `SAP rejected the gateway's credentials.` | Wrong or expired API key. Copy it again from api.sap.com. |
| `SAP is rate limiting the gateway` | The sandbox throttles requests. Wait a minute. |
| Every SAP order comes back "not found" | A wrong `SAP_BASE_URL` path on `api.sap.com` also answers 404. Reset it to the default in `.env.example`. |
| Alias search returns nothing | SOSL indexing lag. Retry after a few minutes, or search by name. |
| Inspector or curl gets **401** from the gateway | No token, or it expired (5 minutes). Run `make token P=01 CLIENT=cs-rep` again. `docker compose logs gateway` shows the `auth_rejected` reason. |
| `startup_failed ... DATABASE_URL must use TLS for a remote database` | Add `sslmode=require` to the URL in `.env` (Neon's console URL already has it). |
| Every call is denied with `could not be recorded in the audit log` after switching to Neon | The tables are missing (run `make db-init P=01`), or Neon is unreachable. `docker compose logs gateway` shows `"audited": false`. |
| `make ask` prints `memory problem: recall: ...` | The memory database or the embedding server did not answer. The answer is unaffected; memory is skipped for that run. Check `MEMORY_DATABASE_URL` (or the local `db`) and that `make models P=01` finished. |
| `make ask` fails with `dependency failed to start: container ... embeddings ... is unhealthy` | The model file is missing: run `make models P=01` once. |
| Every case request is refused with `could not be recorded in the audit log` after upgrading to Milestone 8 | The approvals table lacks the new `trace_id` column. Hosted: `make db-init P=01` (and `make db-init P=01 TARGET=test`). Local: `docker compose -f projects/01-enterprise-mcp-gateway/docker-compose.yml exec -T db psql -U postgres -d ridgeline -f - < projects/01-enterprise-mcp-gateway/db/audit.sql` (safe to repeat). |
| The console shows "This console needs its link" | Open the link `make console-up P=01` prints; it sets the session cookie. |
| Wire tests are all skipped with `the gateway's database is unknown` on `DB=local` | The local volume was created before Milestone 7, so it was never stamped. Stamp it: `docker compose -f projects/01-enterprise-mcp-gateway/docker-compose.yml exec -T db psql -U postgres -d ridgeline -v environment=local -f - < projects/01-enterprise-mcp-gateway/db/stamp.sql`, then `make up P=01 DB=local` (or delete the `ridgeline-p01_db-data` volume to start fresh). |
| Wire tests are all skipped with `the gateway's database is main` | By design: the stack is on the Neon main branch. Restart it with `make up P=01 DB=test` (or `DB=local`). |
| `make eval LLM=claude` stops with `set ANTHROPIC_PRICE_IN_PER_MTOK` | The optional hosted model needs its settings in `.env` (section 7). The free local model needs none. |
| `make db-roles` stops with `has an elevated attribute` | One of the roles was created another way (console, CLI) with extra rights. Drop it as the owner (`DROP ROLE ...`) and run `make db-roles P=01` again. |
| `make up` stops with `required variable KC_ADMIN_PASSWORD is missing` | `.env` was created before Milestone 3 and the backfill didn't run. Run `make up P=01` from the repo root; it adds the missing keys. |
