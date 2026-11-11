# Gateway (MCP server)

The Ridgeline enterprise MCP gateway is the one controlled **environment** that every agent in the portfolio acts through. It is built on the official MCP Python SDK (`mcp==2.3.0`, `MCPServer`, which was called FastMCP before SDK 2.x) and serves **streamable HTTP** at `/mcp`. All data is synthetic (fictional Ridgeline Supply Co.).

## Status: Milestone 5 (guardrails, audit, approvals). Every call needs a Keycloak token and an allow from OPA, is rate limited and audited; the one write tool runs only after a human approves it
| | Now | Arrives in |
|---|---|---|
| Tools | Four read-only tools: `search_accounts`, `get_sales_order`, `track_shipment`, `get_open_invoices`, and one write, `create_case`, which runs only after a human approves it. They keep the copilot's names, arguments and output shapes. | |
| Connectors | One per system, each with a `seed` backend (offline synthetic data) and a live backend, chosen in `.env`. The table below lists them. | |
| Choke point | Every `tools/call` goes through `GatewayServer.call_tool`. In order: rate limit, unknown tools rejected, OPA (scope check, then data check), unexpected arguments rejected, the tool (a write asks a human), PII masking, then one audit row and one log line for every outcome. | |
| Input validation | Closed JSON Schemas (`additionalProperties: false`):<br>• `sales_order` must match `^SO-[0-9]{6}$`<br>• `account_id` must match `^ACCT-[0-9]{4}$`<br>• `query` must match `^[A-Za-z0-9][A-Za-z0-9 &.,'-]{1,79}$`<br>The gateway accepts only canonical IDs; clients normalise case and whitespace. | |
| Secrets | Read only from the environment and masked in every repr. Each container gets only its own variables. A half-configured live backend stops startup (fail closed). | |
| Writes | One: `create_case`, after an explicit human approval, into the seeded CRM. The connectors to live systems still send only `GET` (plus the Salesforce token `POST`), enforced in `HttpConnector`. The operator seed tool alone builds a `POST`/`PATCH` Salesforce connector, and it never sends `DELETE` or `PUT`. | Optional: write approved cases to the Salesforce dev org |
| Transport security | Published on `127.0.0.1` only. Host and Origin allowlist (a bad Host gets 421, a bad Origin 403). The containers are non-root and read-only, with all capabilities dropped and `restart: "no"`. The mock APIs sit on an internal-only Docker network (`internal: true`: no route to the host or the internet). Images are local builds only (`pull_policy`). | |
| Identity | **Required.** A Keycloak access token on every `/mcp` call (`auth.py`). The gateway checks:<br>• RS256 signature against Keycloak's keys<br>• exact `iss` and `aud` = `ridgeline-gateway`<br>• `typ` Bearer and an `azp` client id<br>• `exp` exactly; 5 s clock leeway on `nbf`/`iat`<br>Anything else gets 401 with a `WWW-Authenticate` challenge pointing to `/.well-known/oauth-protected-resource/mcp`. It fails closed: an unverifiable token gets 401, cached keys are dropped after Keycloak has been unreachable for 1 hour, and the gateway won't start without `OIDC_*`. | Consider `stateless_http=True` with per-request JWTs |
| Authorization | **Required.** OPA decides every call (`policy.py`, `../policy/gateway.rego`): per-tool scopes, a region data rule, default deny, fail closed. `tools/list` shows only the tools a caller's scopes cover. The scope-less `p01-expiry-test` client is denied every tool. See [Authorization (OPA)](#authorization-opa). | |
| Observability | One audit row and one JSON line per call: `{"event": "tool_call", "tool", "outcome", "ok", "latency_ms", "client_id", "scopes", "regions", "policy_ms", "policy_reason"?, "hidden_out_of_region"?, "pii_masked"?, "approval_id"?, "audited"?, "backend", "upstream_status"?, "upstream_ms"?, "upstream_calls"?, "request_id", "traceparent"?, "x_request_id"?, "error_type"?}`.<br>• Outcomes: `ok`, `denied`, `not_found`, `invalid_arguments`, `unknown_tool`, `upstream_error`, `error`.<br>• A denial carries the policy's reason code in `policy_reason`. `backend` and the `upstream_*` fields describe the tool's own system; policy queries and region lookups are not counted there.<br>• Not logged: argument values (the audit row stores them masked), unknown tool names, malformed trace headers. SDK and HTTP-client INFO logs are silenced.<br>• Clients see only `Error executing tool <name>` for crashes and a sanitized message for upstream failures. | |

### Connectors
| Tool | System | `seed` | Live backend (`.env`) | Auth | Mapping notes |
|---|---|---|---|---|---|
| `search_accounts` | CRM | `mock_systems` | `CRM_BACKEND=salesforce`: Developer Edition REST API | OAuth 2.0 client credentials against the My Domain URL (`*.my.salesforce.com`, https, default port). Token cached 15 min, refreshed once on 401 (never on 403). The returned `instance_url` must also be a My Domain host before the token is sent there. | Standard fields only: `AccountNumber`=ACCT-xxxx, `Site`=region, `Description` lines `Type:` and `Aliases:`, first Contact = primary contact. SOQL (name/number) plus SOSL (aliases), both escaped. |
| `get_sales_order` | ERP | `mock_systems` | `ERP_BACKEND=sap_sandbox`: SAP Business Accelerator Hub `API_SALES_ORDER_SRV` (OData v2) | `APIKey` header | SAP's demo data, not Ridgeline's. SAP `1` maps to `SO-000001`; orders above 999999 can't be addressed. Same keys as a seed order, `null` where SAP has no equivalent (`account_id`, `account_name`, `ship_from`, `service`). `RequestedDeliveryDate` maps to `promised_delivery`; `unit_price` comes from `NetPriceAmount` only when the price is per 1 unit. SAP-only fields go under `source`. |
| `track_shipment` | Carrier | `mock_systems` | `CARRIER_BACKEND=http`: `mock_apis` carrier service (Compose default) | `X-API-Key` | Same shape as the seed backend |
| `get_open_invoices` | AR | `mock_systems` | `AR_BACKEND=http`: `mock_apis` AR service (Compose default) | `Authorization: Bearer` | Every item is validated (dict, same account, numeric amount). The gateway computes `count` and `total_open` in `Decimal` |

Every connector request is pinned to its host and port, never follows redirects, uses `HTTP_TIMEOUT_S` (default 10) and streams bodies, stopping as soon as one passes 5 MB (never buffered whole). Connection pools close on shutdown. Errors map to sanitized `ConnectorError` messages:
- credentials rejected (401)
- refused (403)
- rate limited (429)
- HTTP status
- unreachable
- timed out
- too large
- unreadable or unexpected payload

Live sandbox setup (Salesforce External Client App, SAP API key): [../docs/live-sandbox-setup.md](../docs/live-sandbox-setup.md).

## Layout
```
gateway/
  ridgeline_gateway/server.py        GatewayServer (MCPServer subclass), the five tools, /healthz, /audit,
                                     /approvals/callback, /approvals/pending, startup
  ridgeline_gateway/audit.py         audit store (Postgres via asyncpg; in-memory for tests), AuditConfig, fail closed
  ridgeline_gateway/approvals.py     human approval through n8n: request, wait, one-time callback, timeout
  ridgeline_gateway/pii.py           masking of contact emails and phones in results and audit arguments
  ridgeline_gateway/ratelimit.py     per-client sliding one-minute window
  ridgeline_gateway/config.py        settings from env, Secret, fail-closed validation
  ridgeline_gateway/auth.py          Keycloak JWT verifier (TokenVerifier), AuthConfig, principal (client, scopes, regions)
  ridgeline_gateway/policy.py        OPA client (fail closed), decisions, denial messages, PolicyConfig
  ridgeline_gateway/connectors/      base (HttpConnector, errors, upstream info), seed, salesforce, sap, http_mocks
  ridgeline_gateway/admin/           live_check (all tools through MCP, as cs-rep), token (make token),
                                     seed_salesforce (synthetic accounts), audit_report (make audit),
                                     approvals (make approvals), request_case (make request-case)
  tests/                             config, connectors (httpx2.MockTransport), tool contracts + logging,
                                     identity, authorization against a real OPA (opa_fixture.py),
                                     admin tools (seed_salesforce against a fake org, live_check),
                                     governance (audit, rate limits, PII, approvals against a fake n8n)
  pyproject.toml, requirements.lock  top-level pin; full hashed closure (linux amd64 + arm64)
  Dockerfile                         digest-pinned python:3.12-slim; stages base -> test -> runtime;
                                     the test stage adds the pinned OPA binary and ../policy
../policy/                          gateway.rego, system_authz.rego, data.json, decision_cases.json, gateway_test.rego
../mock_apis/                        FastAPI carrier + AR services, own lock and Dockerfile, tests over real HTTP
../lock-deps.sh                      ./lock-deps.sh gateway | mock_apis | copilot
../docker-compose.yml                gateway on 127.0.0.1:${GATEWAY_PORT:-8765}; keycloak on 127.0.0.1:${KEYCLOAK_PORT:-8080};
                                     carrier-api, ar-api internal only; opa on its own internal network;
                                     db (Postgres + pgvector) on the internal `data` network; n8n on 127.0.0.1:${N8N_PORT:-5678};
                                     ops (profile) for live-check/token/audit/approvals;
                                     copilot + embeddings (profile "copilot") for `make ask`
../db/audit.sql, memory.sql, init.sh  audit schema (idempotent, append-only triggers), the copilot's memory
                                     schema, local role setup; roles.py: least-privilege roles in Neon
../n8n/ridgeline-approval.json       the approval workflow, imported and published at every n8n start
../keycloak/ridgeline-realm.json     realm, client scopes, clients with a regions claim (secrets are ${ENV} placeholders)
../tests/test_gateway_http.py        stdlib wire-level MCP tests against the running stack
```

## Run it (Docker Desktop)
```bash
make up P=01             # first run creates .env from .env.example with random mock-API secrets; waits until healthy
curl -s localhost:8765/healthz
make opa-test P=01       # Rego unit tests + format check, in the pinned OPA image
make gateway-test P=01   # builds each image's `test` stage and runs it (no network); does not redeploy
make up P=01 DB=test     # the stack on the Neon test branch (or DB=local): the wire tests need one of them
make test P=01           # includes the raw JSON-RPC-over-HTTP contract tests while such a stack is up
REQUIRE_GATEWAY=1 make test P=01   # CI: fail instead of skipping when the gateway is down or on main
make live-check P=01     # every tool through MCP; result per tool with its client and configured backend
make audit P=01          # audit report (as p01-auditor, the only identity with audit.read)
make approvals P=01      # pending write requests and their approval links (operator token)
make request-case P=01   # as cs-lead, request a case and wait for the human decision
make down P=01           # the audit log and n8n's database stay in Docker volumes
```
If you change `GATEWAY_PORT` in `.env`, export it too: Compose reads `.env`, but `make test` does not. With live CRM/ERP backends the wire tests detect it (the seeded order SO-104233 is not in the SAP sandbox) and skip the seed-data comparisons: SAP sandbox orders have no Ridgeline account, so the rep is denied them. `GATEWAY_LIVE_BACKENDS=1` or `=0` overrides the detection.

## Identity (Keycloak)
- Keycloak 26.8.2 is pinned by digest and published on `127.0.0.1:${KEYCLOAK_PORT:-8080}`. It runs in dev mode: no TLS, H2 database, loopback only.
- Realm `ridgeline` is imported from `../keycloak/ridgeline-realm.json` on every start, with secrets substituted from `.env`.
- Admin console: `http://localhost:8080/` (or your `KEYCLOAK_PORT`), user `admin`, password = `KC_ADMIN_PASSWORD` in `.env`.
- Hardening:
  - Agent clients have only the service-account (client credentials) grant: standard, implicit, password, device, CIBA and token-exchange flows are off.
  - `fullScopeAllowed` is false, there are no realm default scopes and no users, and tokens last 5 minutes with no refresh tokens.
  - `bruteForceProtected` only guards user logins. Client secrets are 192-bit random values.
  - `sslRequired: external` is fine on loopback; set it to `all` before exposing Keycloak anywhere else.

| Client | Scopes in its token | `regions` claim | Use |
|---|---|---|---|
| `cs-rep` | `crm.read erp.read shipping.read ar.read` | `["Central"]` | the copilot acting for a Central-region rep; `make live-check` |
| `cs-lead` | the four reads plus `crm.write` | `["*"]` (every region) | the copilot acting for a lead (case writes need approval, M5) |
| `p01-expiry-test` | none (audience only), 5-second tokens | none | wire tests: an expired token gets 401; a token without scopes is denied every tool |
| `p01-crm-only-test` | `crm.read` | `["Central"]` | wire test: a partly scoped agent sees and reaches only `search_accounts` |

The `regions` claim is a hardcoded-claim mapper on each client, so it is signed into the token like the scopes. One client stands for one desk here; a deployment with many reps would issue the claim per user or per desk.

Every token's issuer is `http://localhost:${KEYCLOAK_PORT:-8080}/realms/ridgeline` (`KC_HOSTNAME`), whether requested from your Mac or from a container. The gateway fetches signing keys internally from `http://keycloak:8080/.../certs`.

Keycloak's database is in memory, so every Keycloak restart re-imports the realm with **new signing keys**. After restarting only Keycloak, wait up to 60 s (the gateway's unknown-key refetch limit) or restart the gateway too; `make up` handles both.

`make token P=01 CLIENT=cs-rep` prints a 5-minute token. It runs in the `ops` container, which holds the client secrets; the gateway container never does.

## MCP Inspector
Every call needs a token now. In the web UI:
1. Run `make token P=01 CLIENT=cs-rep` and copy the output.
2. Open **Authentication**, set **Header Name** to `Authorization` and **Bearer Token** to the token. Or add a custom header: `Authorization: Bearer <token>`.
3. Click **Connect**.

The token expires after 5 minutes; get a new one and reconnect. In the CLI, pass the header with `--header "Authorization: Bearer $TOKEN"`.
Without a token, the Inspector follows the 401 challenge to Keycloak and offers an interactive OAuth sign-in. That can't work here by design: the realm has no users, only agent machine identities (Keycloak's built-in account and admin consoles exist but have nobody to log in). The CLI reports it as `auth_required`. Paste a token instead.
```bash
TOKEN=$(make -s token P=01 CLIENT=cs-rep)          # from the repo root; stays in this shell only
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http \
  --header "Authorization: Bearer $TOKEN" --method tools/list
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http \
  --header "Authorization: Bearer $TOKEN" --method tools/call --tool-name search_accounts --tool-arg query=BBO
npx -y @modelcontextprotocol/inspector@2.14.0 --web --transport http --server-url http://localhost:8765/mcp
```
Try these on the seed data:
- As `cs-rep` (Central): `search_accounts` `Summit`; `get_sales_order` and `track_shipment` `SO-104233`; `get_open_invoices` `ACCT-1002`. All return data.
- As `cs-rep`, outside its region: `get_open_invoices` `ACCT-1003` or `get_sales_order` `SO-104301` (Pinecrest Hardware, West) returns `Access denied: ... outside your region`; `search_accounts` `Pinecrest` returns no matches and `hidden_out_of_region: 1`.
- As `cs-lead` (`CLIENT=cs-lead`): all of the above return data, and `search_accounts` `BBO` finds Big Box Outdoor (East).

## Authorization (OPA)
Open Policy Agent 1.22.2 (digest-pinned, static image) is the policy decision point; the gateway enforces. The policy is [`../policy/gateway.rego`](../policy/gateway.rego) with its data in `../policy/data.json`. Default deny.

| Rule | What it does |
|---|---|
| Scope | A tool runs only if the caller's token holds the scope `data.json` maps it to: `search_accounts` → `crm.read`, `get_sales_order` → `erp.read`, `track_shipment` → `shipping.read`, `get_open_invoices` → `ar.read`, `create_case` → `crm.write`. A tool that is not in the map is denied. |
| Restricted changes | Pricing, credit-limit and payment-term changes are always denied, by tool name and by argument name on write tools. |
| Region (data rule) | A record is returned only if its account's region is one of the caller's `regions`. `cs-rep` is a Central-region rep; `cs-lead` holds `*` and sees every region. |
| Unknown region | A record with no Ridgeline account, or an account with no region, is denied to reps and visible to leads only. Every SAP sandbox order is in this group. |

How a call is authorized, all in `GatewayServer.call_tool`:
1. **Scope check** before anything runs: `{principal, tool, args}` goes to OPA. The principal (`client_id`, `scopes`, `regions`) comes only from the verified token.
2. **Data check** after the tool has read the record and before anything is returned: the gateway resolves the record's account to its CRM region and asks OPA again with `resource: {region}`.
   - `get_open_invoices`: the account itself. The account is the input, so when its region is known this check runs before AR is read: a caller outside the region causes no AR call.
   - `get_sales_order`: the order's account.
   - `track_shipment`: the order's account, read from ERP (the carrier does not know the customer).
   - `search_accounts`: each match is checked; matches outside the caller's regions are dropped and counted in `hidden_out_of_region`.
3. A tool that returns without asking for the data check is refused (`resource_not_checked`), so a future tool cannot skip the rule by accident.

Fail closed: OPA unreachable, slower than `OPA_TIMEOUT_S` (2 s), answering an HTTP error or anything but a well-formed decision means deny. The gateway refuses to start without `OPA_URL`. A CRM failure while resolving a region returns the upstream error and no data.

What a denied caller sees is a normal MCP tool error (`isError: true`), with text built by the gateway from the reason code:

| Reason code (logged as `policy_reason`) | Text |
|---|---|
| `missing_scope` | `Access denied: cs-rep lacks scope crm.write for create_case.` |
| `out_of_region` | `Access denied: this record belongs to an account outside your region (cs-rep may see: Central). Ask a CS lead.` |
| `region_unknown` | `Access denied: this record is not linked to an account with a known region, so only a CS lead may read it.` |
| `restricted_change` | `Access denied: pricing, credit-limit and payment-term changes are human-only.` |
| `unknown_tool` | `Access denied: no policy authorizes <tool>.` |
| `policy_unavailable`, `policy_error`, `invalid_request`, `resource_not_checked` | `Access denied: the policy service could not authorize this call, so it was refused.` |

**Tradeoff: a region denial says "access denied", not "not found".** That tells a rep the record exists. It is deliberate: reps are internal users, the message tells them to hand the request to a CS lead, and the copilot can route it instead of telling a customer the order does not exist. A gateway facing external callers should answer "not found" instead. A record that does not exist is still "not found" for everyone.

**`tools/list` is filtered** with the same policy (`allowed_tools`): a caller sees only the tools its scopes cover, and never a restricted or unmapped tool. That is least privilege in discovery, not the control; a hidden tool called directly is still denied at call time.

OPA itself:
- It sits on its own internal Docker network that only the gateway joins. It is not published to the host.
- `--authorization=basic` with [`../policy/system_authz.rego`](../policy/system_authz.rego): its API answers only the two decision queries and `/health`. Policies and data cannot be read or replaced over the network; they change by editing `../policy/` and restarting.
- Decision logging is off (inputs hold tool arguments). The gateway's audit table and call log are the record.
- The container is read-only with all capabilities dropped. Its healthcheck evaluates the mounted policy, so a policy that does not compile keeps the gateway from starting.

Changing the policy:
1. Edit `../policy/gateway.rego` or `data.json`, and add the case to `../policy/decision_cases.json`.
2. `make opa-test P=01` runs the Rego unit tests and `opa fmt` in the pinned image.
3. `make test P=01` runs the same decision table against the copilot's offline copy of the rules (`cs_copilot/agent/guardrails.py`), so the two cannot drift.
4. `make gateway-test P=01` runs the gateway against a real OPA with the edited policy; `make up P=01` restarts the stack with it.

## Guardrails, audit and approvals
Milestone 5. Agents propose; a human approves.

**Approvals.** `create_case` is the gateway's only write. After the rate limit, scope, restricted-change and region checks pass:
1. The gateway records the request in the `approvals` table. If that fails, the write is refused (`audit_unavailable`).
2. It posts the request to the n8n workflow (`../n8n/ridgeline-approval.json`) with the shared secret `X-Ridgeline-Approval` (`APPROVAL_WEBHOOK_SECRET`; n8n refuses requests without it): request id, a one-time callback token, the client, the tool, the account, the case text and how long it will wait. Subject and summary allow only plain text, so they cannot carry markup into the form.
3. The workflow answers with a signed link to an approval form and waits, at most as long as the gateway does. The gateway accepts the link only if it is on `N8N_FORM_BASE_URL` and carries n8n's signature.
4. A person opens the link (`make approvals P=01`), chooses Approve or Reject and gives their name.
5. The workflow posts `{request_id, token, decision, approver}` to `/approvals/callback` at the gateway's fixed internal address. A request id is resolved once, and only with its own token.
6. Only `Approve` from a named person writes the case. The result names the approval and the approver.

| What happens | Reason code | Written |
|---|---|---|
| Approved | (outcome `ok`) | yes |
| Rejected, or anything but an explicit named Approve | `approval_denied` | no |
| No decision within `APPROVAL_TIMEOUT_S` (default 120 s) | `approval_timeout` | no |
| n8n unreachable, an HTTP error, a wrong request id, a form link on another host | `approval_unavailable` | no |
| The request or the approval cannot be recorded (incl. a request another process already closed) | `audit_unavailable` | no |
| The caller cancels or disconnects while waiting | outcome `cancelled`; the approval is closed as `timed_out` | no |

The approval link is the approver's credential: no tool returns it, it is not logged, and it is not stored. `GET /approvals/pending` lists it, and needs `APPROVAL_OPERATOR_TOKEN`, which no agent client holds, so an agent cannot approve its own request.

How n8n is protected:
- **Network.** n8n sits on the internal `approvals` network, which only the gateway shares, plus a network that only publishes the form on `127.0.0.1`. An agent container cannot resolve or reach it.
- **Editor and data.** The editor needs the owner account (create it on first start: [setup guide, section 5b](../docs/live-sandbox-setup.md#5b-approvals-create-the-n8n-owner-account-milestone-5-once)), its public API is off (`N8N_PUBLIC_API_DISABLED`), and finished executions, which hold case text, are pruned after 7 days.
- **What the form proves.** The form has no login of its own: whoever holds the signed link can decide, and the approver name is what they type. It expires when the gateway stops waiting. Its completion page says the decision was *sent*; the requester's terminal and `make audit` show whether it was applied.

**Audit.** One row per authenticated tool call in Postgres (`audit_log`), whatever the outcome, cancelled calls included (a request without a valid token never becomes a tool call; it gets 401 and an `auth_rejected` log line): client, scopes, regions, tool, outcome, reason code, argument values with PII masked and long text cut, latency, backend, upstream status, JSON-RPC request id, trace id, approval id, how many values were masked in the result. Approvals have their own table with status, approver and timestamps.
- The gateway's database role (`gateway_audit`, locally and in Neon) can insert and read audit rows but not update or delete them. It may set only the decision columns of an approval.
- At startup the gateway closes any approval request a previous process left `pending` (it stopped while a human was deciding) as `unavailable`. Nothing was written for it. A late form submission reaches no waiting request (the gateway answers n8n with 404) and changes nothing.
- A read that cannot be audited is not returned (`audit_unavailable`). An approved write is already recorded in `approvals`, so it is reported, and the log line says `"audited": false`.
- `GET /audit?limit=N` needs a valid token with `audit.read`, held only by the operator identity `p01-auditor` (no agent): totals by outcome and by reason, recent calls, recent approvals. `make audit P=01` prints it.
- Postgres (with pgvector, since Milestone 6) runs on an internal network shared by the gateway and the copilot. The copilot's role reaches only its own `memory` schema, and the gateway's role cannot read it. Its data survives `make down`; `docker volume rm ridgeline-p01_db-data` resets it.
- Or keep the audit log in a hosted Postgres such as Neon: set `DATABASE_URL` in `.env` (TLS required for any remote host), run `make db-init P=01` once and `make db-roles P=01` (least-privilege `gateway_audit` and `copilot_memory` roles, written into `.env`), then `make up`. The schema (`../db/audit.sql`) is the same file the local container runs; its triggers keep the log append-only even for the table owner. Setup: [Neon](../docs/live-sandbox-setup.md#6-optional-keep-the-audit-log-and-the-copilots-memory-in-neon-milestones-5-6).
- `make audit P=01 ARGS=--trace=<id>` shows only the calls one copilot run made: the copilot sends its trace id as `x-request-id`, and each audit row stores it.
- Each database is stamped with the environment it serves (`main`, `test` or `local`, `../db/stamp.sql`). The gateway reads the stamp at startup, logs it, and reports it in `/healthz` as `"database"` (a label only, never a host). The wire tests refuse to run unless it is `test` or `local`, so automated tests never write to the Neon main branch: `make up P=01 DB=test` runs the stack on the Neon `test` branch, `DB=local` on the local Postgres ([setup](../docs/live-sandbox-setup.md#6b-a-neon-test-branch-for-automated-tests-milestone-7)).

**PII masking.** Contact emails, phone numbers and SSNs in results are replaced with `[EMAIL REDACTED]` / `[PHONE REDACTED]` / `[SSN REDACTED]` unless the token holds `crm.pii` (cs-lead only). Only string values change, so JSON numbers and record IDs are untouched; a bare 7-digit number is masked only in the `555-0142` form, so `PRO 123.4567` stays. Audit rows store arguments masked for every caller, and the stored approval subject is masked too. The copilot (agent-core) uses the same patterns; `tests/test_milestone0.py` fails if they drift.

**Rate limits.** A sliding one-minute window per verified `client_id`, checked before policy or any backend: `RATE_LIMIT_PER_MINUTE` (default 120), with per-client overrides in `RATE_LIMIT_OVERRIDES`. The test-only `p01-rate-test` client gets 5. Over the limit the caller gets `Access denied: <client> is over its call rate limit`, audited as `rate_limited`. The counters live in the gateway process; several replicas would need a shared store.

**Reason codes.** `../policy/data.json` holds the one list: `reasons` (what OPA may return) and `gateway_reasons` (the gateway's own fail-closed codes). OPA, the gateway and the copilot all read it, and each side has a test that fails if a code is missing or has no explanation for the caller.

## Updating dependencies
1. Edit the top-level pins in `gateway/pyproject.toml` or `mock_apis/pyproject.toml`.
2. From the project root, run `./lock-deps.sh gateway` (or `mock_apis`). It resolves inside the Dockerfile's digest-pinned base image and hashes the wheels for amd64 and arm64.
3. Run `make gateway-test P=01` and `make up P=01`.

To move the base image, update the digest in both Dockerfiles (`docker pull python:3.12-slim`, then `docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim`). To move OPA, update the image and digest in `../docker-compose.yml` and `gateway/Dockerfile` together (`make opa-test` reads the Compose pin).

## Notes for later milestones
- `gateway:*` is in `GATEWAY_ALLOWED_HOSTS`, so containers on the project network (`ops`, n8n, the copilot) can call the gateway. `/mcp` still needs a token; n8n uses only `/approvals/callback`.
- Optional: write approved cases to the Salesforce dev org (`CRM_BACKEND=salesforce`). That needs Case create permission for the connected app's Run As user and a write-capable connector for that one endpoint.
- The Salesforce Run As user is the identity behind every CRM call. The setup guide recommends a dedicated minimum-access integration user now. The gateway's connector is GET-only either way.

## The copilot as a client (Milestone 6)
`make ask P=01 Q="..."` runs the copilot (`../cs_copilot/`) in its own container as an MCP client of this gateway: `McpToolSource` (official SDK, streamable HTTP) registered through `agentcore.register_tool_source`. It authenticates as the asking role (`cs-rep` or `cs-lead`, Keycloak client credentials) and sends its trace id as `x-request-id`. The client's policy data decides scopes and side effects; this server's tool descriptions are never trusted for that. Its state snapshot reads through the same tools, so context reads are authorized and audited too. A lead's `create_case` waits here for the human decision in n8n, like `make request-case`. Offline (demo, tests, scripted evals) the copilot still uses its in-process tools over the mock systems. Details: [../docs/agent-architecture.md](../docs/agent-architecture.md#the-copilot-on-the-gateway-milestone-6).
