# Enterprise MCP Tool Gateway + Customer Service Copilot

> Part of the [Agentic Integration Portfolio](../../README.md): enterprise AI agents for a fictional company, Ridgeline Supply Co. All data is synthetic.

**Business operation:** Customer Service · IT Platform

A governed MCP gateway that gives every Ridgeline agent secure, scoped, audited access to ERP, CRM, carrier and AR systems, plus a Customer Service Copilot built on it.

## The business problem
Ridgeline's customer service reps answer "where is my order, my shipment and my invoice?" about 1,500 times a week by switching between the ERP, CRM, carrier portals and the AR system. Leadership wants an AI copilot. Security will only approve it if no agent can reach an enterprise system without identity, least-privilege policy, rate limits and an audit trail. This project builds that governed **MCP gateway**, which every other agent in the portfolio uses, and a **Customer Service Copilot** on top of it.

## Architecture
```mermaid
flowchart TB
    IN["Input<br/>Customer Service · IT Platform events and requests"] --> PER["PERCEIVE<br/>CaseRequestPerceiver"]
    PER --> CTX["HARNESS: context injection<br/>state + memory + RAG + skills"]
    CTX --> LLM["THINK<br/>cs-copilot"]
    LLM <--> MEM[("REMEMBER<br/>memory + state")]
    RAG[("RAG<br/>knowledge/*.md")] --> CTX
    SK[["SKILLS<br/>skills/*/SKILL.md"]] --> CTX
    LLM --> Q1{"Can I answer<br/>directly?"}
    Q1 -- yes --> OUT["Output"]
    Q1 -- no --> PLAN["PLAN<br/>ordered sub-tasks"]
    PLAN --> ACT
    ACT["ACT via MCP / APIs / code<br/>search_accounts, get_sales_order, track_shipment<br/>get_open_invoices, create_case"]
    ACT -- results --> Q2{"Goal achieved?<br/>EvidenceBackedAnswerVerifier"}
    Q2 -- yes --> OUT
    Q2 -- "no, within budget" --> LLM
    GR["GUARDRAILS<br/>scopes, policy, approvals, budgets, PII masking"] -.-> ACT
    GR -.-> OUT
    OBS["OBSERVE<br/>traces, cost, latency, evals"] -.-> LLM
    OBS -.-> ACT
    OUT -.-> A2A["A2A Agent Card<br/>discoverable by other agents"]
```

## Agentic AI concepts demonstrated
| # | Concept | How this project uses it |
|---|---------|--------------------------|
| 1 | Memory & State | Long-term: answered questions, account aliases (pgvector). State: the current case and its open orders, refreshed every iteration. |
| 2 | Orchestration | The gateway is the controlled **environment** that every manager and micro agent in Projects 02–08 acts through. |
| 3 | RAG | Customer service policy and escalation matrix are retrieved and cited (`[customer-service-policy.md#0]`). |
| 4 | Harness | agent-core loop: context injection (policy, skills, memory), compaction for long chats, progress files per conversation. |
| 5 | Evals | `evals/cases.json`: WISMO, billing dispute, policy questions, permission denials, out-of-scope refusals. |
| 6 | MCP | **Core of this project.** The gateway is an MCP server; the copilot connects through an MCP client (`McpToolSource` via `register_tool_source`). |
| 7 | Skills | `where-is-my-order`, `billed-not-delivered` runtime skills, loaded by trigger or `load_skill`. |
| 8 | A2A | The copilot publishes an Agent Card so the Command Center (Project 08) can delegate to it. |
| 9 | Multi-Agent System | One gateway serves many specialist agents, each with its own identity and scopes. |

## Run it
```bash
make up P=01            # from the repo root: create/backfill .env, build + start gateway, Keycloak, OPA, mock APIs (Docker Desktop)
make opa-test P=01      # Rego unit tests + format check for policy/, in the pinned OPA image
make gateway-test P=01  # gateway + mock-API tests, inside their images' test stages (real OPA binary)
make test P=01          # includes wire-level MCP + identity tests while the stack is up (skipped otherwise);
                        # live Salesforce/SAP backends are detected and the seed-only comparisons skipped
make live-check P=01    # call all four tools through MCP as cs-rep (real Keycloak token, real OPA decision)
make token P=01 CLIENT=cs-rep   # print a 5-minute access token, e.g. to paste into MCP Inspector
make arch-check P=01
make demo P=01          # offline demo with a scripted LLM; prints every span by layer
make eval P=01          # scripted evals vs evals/baseline.json
```

## Status
### Roadmap
| # | Milestone | Scope | Status |
|---|---|---|---|
| 0 | Architecture skeleton | Perceiver, state provider, tools on local mocks and goal verifiers, so the agent runs end to end on agent-core with a scripted LLM | Done |
| 1 | MCP server skeleton | Gateway MCP server with one tool in Docker Compose, reachable from MCP Inspector | Done |
| 2 | Connectors | Salesforce (OAuth), SAP sandbox (API key), mock carrier and mock AR APIs behind gateway tools | Done |
| 3 | Authentication | Keycloak realm with `cs-rep` and `cs-lead` clients; the gateway validates JWTs | Done |
| 4 | Authorization with OPA | Per-tool scope policies and a data rule (reps only see accounts in their region) | Done |
| 5 | Guardrails, audit, approvals | Rate limits, PII masking, audit table and `/audit` report; n8n approval webhook for `create_case`, denying on timeout | Planned |
| 6 | Copilot on the gateway, long-term memory | The copilot calls the gateway through an MCP client; RAG and skills; Postgres + pgvector memory | Planned |
| 7 | Evals and A2A card | Real-model eval baseline (Ollama); publish the copilot's Agent Card | Planned |

**Milestone 4 (authorization with OPA): done.** A valid token is no longer enough. Open Policy Agent 1.22.2 runs in Docker Compose and decides every tool call from the scopes and regions in the caller's Keycloak token; the gateway enforces the decision and denies when it cannot get one. Policy: [`policy/gateway.rego`](policy/gateway.rego). Details: [gateway/README.md](gateway/README.md#authorization-opa).

| Rule | Effect |
|---|---|
| Per-tool scopes | Each tool needs its own scope (`search_accounts` → `crm.read`, `get_sales_order` → `erp.read`, `track_shipment` → `shipping.read`, `get_open_invoices` → `ar.read`, `create_case` → `crm.write`). A tool the policy does not know is denied |
| Restricted changes | Pricing, credit-limit and payment-term changes are denied for everyone |
| Region data rule | Reps only see accounts in their region, and the orders, shipments and invoices of those accounts. `cs-rep` is a Central-region rep; `cs-lead` sees every region |
| Unknown region | A record with no Ridgeline account (every SAP sandbox order) is denied to reps and visible to leads only |

An unauthorized call gets a clear MCP tool error, for example:
```text
cs-rep           create_case        Access denied: cs-rep lacks scope crm.write for create_case.
p01-expiry-test  search_accounts    Access denied: p01-expiry-test lacks scope crm.read for search_accounts.
cs-rep           get_open_invoices  ACCT-1003 (Pinecrest Hardware, West)
                                    Access denied: this record belongs to an account outside your region
                                    (cs-rep may see: Central). Ask a CS lead.
cs-rep           search_accounts    "Pinecrest"  ->  {"matches": [], "hidden_out_of_region": 1}
cs-lead          get_open_invoices  ACCT-1003    ->  1 open invoice
```

**Tradeoff: "access denied" discloses that a record exists.** A rep who asks for an order outside their region learns that the order is real. That is deliberate here: reps are internal users, the message tells them who can help, and the copilot can hand the request to a CS lead instead of telling a customer the order does not exist. A gateway serving external callers should answer "not found" instead. A record that does not exist is still "not found" for everyone. Search goes one step further: `hidden_out_of_region` counts the matches a rep may not see, so a rep can learn that an account with a given name exists in another region. That is accepted for internal reps for the same reason, and the Milestone 5 rate limits bound how fast anyone can probe it.

| Authorization (Milestone 4) | What works now |
|---|---|
| Guardrails | Every `tools/call` is checked twice in `GatewayServer.call_tool`, both before anything is returned:<br>• **the call:** `{principal, tool, args}` → scope rule and restricted-change rule;<br>• **the data:** the record's account is resolved to its CRM region → region rule.<br>A tool that returns without asking for the data check is refused. The principal comes only from the verified token.<br>**Fail closed:** OPA unreachable, slower than 2 s, or answering anything but a well-formed decision means deny; the gateway will not start without `OPA_URL`; a CRM failure while resolving a region returns no data |
| MCP | Denials are ordinary tool errors (`isError: true`) with text built from the policy's reason code. `tools/list` shows a caller only the tools its scopes cover: `cs-rep` sees the four read tools, a `crm.read`-only client sees `search_accounts`, a client without scopes sees none. Call-time enforcement is the control; a hidden tool called directly is still denied |
| Multi-Agent / Orchestration | Each agent identity has its own scopes and a signed `regions` claim (a mapper on its Keycloak client). The scope-less `p01-expiry-test` client is denied every tool, as Milestone 3 promised |
| OPA hardening | Digest-pinned static image on its own internal network that only the gateway joins; read-only, all capabilities dropped; its API answers only the two decision queries and `/health` (`policy/system_authz.rego`), so policy and data cannot be read or replaced over the network; decision logging off |
| Observe | The call log line adds `regions`, `policy_ms`, outcome `denied` with `policy_reason` (`missing_scope`, `out_of_region`, `region_unknown`, `restricted_change`, `policy_unavailable`, ...) and `hidden_out_of_region` for filtered searches. Still no argument values |
| Copilot (Guardrails, State) | The copilot's policy hook applies the same policy: through OPA when `OPA_URL` is set (deny on error), otherwise an offline copy of the rules that reads the same `policy/data.json`. One decision table (`policy/decision_cases.json`, 55 cases) is run against both the Rego and the copy, so they cannot drift. The state snapshot injected into the prompt is filtered by region too |
| Memory | Long-term memory is partitioned by audience: an answered question is tagged with the caller's scopes and regions and recalled only for a caller with the same ones, so a lead's answer about a West account never reaches a Central rep's prompt. Untagged items are not recalled |
| agent-core | `Principal.attributes` (here `regions`), `EvalCase.attributes` and the opt-in `Agent.memory_audience` recall filter, each with tests |
| RAG, Skills | New `knowledge/data-access-policy.md`; both skills say to stop and escalate to a CS lead on a region denial |
| Tests | **Rego unit tests** (`make opa-test`, plus `opa fmt`): 19 tests covering every tool with and without its scope, unknown and restricted tools, region match / mismatch / missing / unknown, the lead, `allowed_tools`, OPA's own API lock-down and the shared decision table.<br>**Gateway tests** against a real OPA binary serving the committed policy (in the image's test stage): scope and region rules per tool, search filtering, unknown regions, not-found stays not-found, OPA down / failing between the two checks / malformed answers, a tool that skips the data check, `tools/list` per client, identity cannot be supplied in arguments.<br>**Wire tests** with real Keycloak tokens and the stack's OPA: no-scope client sees no tools and is denied all four; the `crm.read`-only client sees and reaches one; `cs-rep` cannot see or call a create-style tool, is denied Pinecrest's records and gets filtered search results; `cs-lead` reads them.<br>**Copilot tests:** decision-table parity, policy data vs. the copilot's tools and the Keycloak realm, region filtering in state, OPA mode failing closed |

Verified:
- `make opa-test`: 19/19 Rego tests pass in the pinned OPA image; `opa fmt` is clean.
- `make gateway-test`: 135 gateway tests (against a real OPA binary) + 9 mock-API tests.
- `REQUIRE_GATEWAY=1 make test P=01` on the default stack (seed CRM/ERP): 74 tests, none skipped, with real Keycloak tokens and the stack's OPA.
- The same command on the live stack (Salesforce + SAP sandbox, detected automatically): 74 tests; 3 seed-only comparisons skipped.
- `make eval P=01`: 10/10, no regression against `evals/baseline.json`.
- `make live-check` on the live stack, each tool called as the least-privileged client that may read the record (SAP sandbox orders have no Ridgeline account, so only the lead may read them):
```text
authenticated as cs-lead, cs-rep (Keycloak client credentials)
tool               client   backend      result  summary   (backend = configured in .env)
search_accounts    cs-rep   salesforce   ok      1 match(es): Summit Contractors LLC (ACCT-1002)
get_sales_order    cs-lead  sap_sandbox  ok      SO-000001 status=Completed total=52.65 USD
track_shipment     cs-lead  carrier_api  ok      1 shipment(s), first status=EXCEPTION
get_open_invoices  cs-rep   ar_api       ok      1 open invoice(s), total_open=8370.0
all tools returned data
```

**Milestone 3 (authentication): done.** Every `/mcp` call now needs a Keycloak access token. Keycloak 26.8.2 runs in Docker Compose with realm `ridgeline`, which has two agent clients using the OAuth 2.0 client credentials flow:
- **`cs-rep`:** `crm.read erp.read shipping.read ar.read`
- **`cs-lead`:** the same four, plus `crm.write`

The gateway validates every token itself and answers **401** when there is no token or the token is expired, tampered, signed by the wrong key or issued for a different audience. The 401 carries a `WWW-Authenticate` header that points to the RFC 9728 metadata naming Keycloak, as the MCP authorization spec expects. Per-tool authorization by scope followed in Milestone 4 (OPA, above), which also denies the scope-less test client `p01-expiry-test` every tool.

| Identity (Milestone 3) | What works now |
|---|---|
| Guardrails | `KeycloakJwtVerifier` (`gateway/ridgeline_gateway/auth.py`), plugged into the MCP SDK's bearer-auth middleware:<br>• **Signature:** RS256 only (`none` and HS256 key-confusion are rejected), checked against Keycloak's signing keys, cached and refetched at most once a minute for an unknown key.<br>• **Claims:** exact `iss`; `aud` contains `ridgeline-gateway`; `typ` is Bearer; `exp`/`iat`/`iss`/`aud` required; `exp` enforced exactly (as the SDK middleware also does); 5 s leeway only for clock skew on `nbf`/`iat`; `azp` client id required.<br>• **Keys:** cached, refetched as a single fetch however many requests wait.<br>• **Fail closed:** any token that can't be verified gets 401.<br>&nbsp;&nbsp;– Keycloak down at startup: no keys, every token is rejected.<br>&nbsp;&nbsp;– Keycloak down later: cached keys keep working for at most 1 hour, then are dropped. The gateway refuses to start without identity configured. `/healthz` and the metadata stay open. |
| Multi-Agent / Orchestration | One machine identity per agent (Keycloak clients with only the service-account grant; browser, password and implicit flows are off; no users). The scope names match the copilot's tools, ready for OPA (M4) and the copilot's MCP client (M6) |
| Secrets | Client secrets and the Keycloak admin password are generated into `.env` by `make up`, which backfills new keys into an existing `.env` without touching your values. They reach Keycloak (through `${ENV}` placeholders in `keycloak/ridgeline-realm.json`) and the one-off `ops` container (`make token`, `make live-check`) only, **never the gateway**, which needs only Keycloak's public keys |
| Observe | Every tool call logs the verified `client_id` and `scopes`. Every refusal logs `{"event": "auth_rejected", "reason": "expired"}` (or `bad_signature`, `wrong_audience`, `unknown_signing_key`, ...), rate-limited to 10 lines per reason per 10 s with a suppressed count. Tokens are never logged; the live logs were checked for JWTs: 0 |
| Tests | **Unit tests** (gateway image, test RSA key + fake key endpoint):<br>• every rejection reason;<br>• strict `exp`;<br>• key caching, the rotation refetch rate limit, and one fetch for 20 concurrent unknown keys;<br>• a malformed key is skipped;<br>• Keycloak unreachable, cold and warm;<br>• log rate limiting;<br>• the HTTP app gives 401 + challenge, 200 with a valid token, 401 on an existing session without a token (POST/GET/DELETE), 404 when another client rides a session, open metadata and health;<br>• startup refuses to run without identity.<br>**Wire tests** with real Keycloak tokens:<br>• no token gives 401 + challenge;<br>• a tampered token, a Basic header or garbage gives 401;<br>• **a real expired token gives 401** (a test-only client issues 5-second tokens; the test waits just past expiry);<br>• `cs-rep` and `cs-lead` carry the right claims and reach all 4 tools;<br>• the metadata names Keycloak. |

Verified on the live stack (Salesforce + SAP sandbox + mock carrier/AR):
- `make gateway-test`: 92 gateway + 9 mock-API tests.
- `REQUIRE_GATEWAY=1 GATEWAY_LIVE_BACKENDS=1 make test P=01`: 50 tests; 2 seed-only comparisons skipped.
- `make live-check` authenticated as `cs-rep`:
```text
authenticated as cs-rep (Keycloak client credentials)
search_accounts    salesforce   ok      1 match(es): Summit Contractors LLC (ACCT-1002)
get_sales_order    sap_sandbox  ok      SO-000001 status=Completed total=52.65 USD
track_shipment     carrier_api  ok      1 shipment(s), first status=EXCEPTION
get_open_invoices  ar_api       ok      1 open invoice(s), total_open=8370.0
```

**Milestone 2 (connectors): done.** The gateway serves the four read tools the copilot declares, each through a connector with a `seed` backend and a live backend chosen in `.env`:
- **CRM:** a Salesforce Developer Edition org, via OAuth 2.0 client credentials.
- **ERP:** the SAP Business Accelerator Hub sandbox, via an API key.
- **Carrier and AR:** the mock FastAPI services in Docker Compose, over authenticated HTTP.

`make live-check` returned data from all four live backends (output below). Secrets stay in the git-ignored `.env`. Setup: [docs/live-sandbox-setup.md](docs/live-sandbox-setup.md). `create_case` (a write) stays off the gateway until approvals in Milestone 5.

| Tool | Connector | Offline (`seed`) | Live backend | Auth | Status |
|---|---|---|---|---|---|
| `search_accounts` | CRM | seeded accounts | Salesforce Developer Edition (SOQL, plus SOSL for aliases such as BBO) | OAuth 2.0 client credentials; token cached, refreshed once on 401; instance URL must be a Salesforce host | fake-transport tested; **live check passed** |
| `get_sales_order` | ERP | seeded orders | SAP Business Accelerator Hub sandbox `API_SALES_ORDER_SRV` (OData v2); SAP `1` maps to `SO-000001` | `APIKey` header | fake-transport tested; **live check passed** |
| `track_shipment` | Carrier | seeded tracking | mock carrier API container | `X-API-Key` | **live in Compose** (default) |
| `get_open_invoices` | AR | seeded invoices | mock AR API container; totals computed by the gateway | Bearer token | **live in Compose** (default) |

| Gateway (Milestone 2) | What works now |
|---|---|
| Act | Connectors are the only code that talks to a backend. Each is pinned to its host and port, is read-only (GET) at the transport, never follows redirects, caps response size and has a timeout. Failures become sanitized `ToolError`s ("SAP rejected the gateway's credentials.", "AR API did not respond in time.") without URLs, bodies or secrets |
| Guardrails | Secrets come only from `.env` (git-ignored, generated with owner-only permissions; random mock-API secrets) and are masked in every repr. Each container gets only its own variables; the mock APIs sit on an internal-only network. A half-configured live backend stops the gateway at startup (fail closed) instead of falling back to seeded data. The mock APIs refuse to start without a strong shared secret and compare secrets in constant time |
| Observe | The call log line adds `backend`, `upstream_status`, `upstream_ms`, `upstream_calls` and the outcome `upstream_error` |
| Tests | 67 gateway tests (config, connectors over fake transports, tool contracts, logging, and the seed tool against a fake Salesforce org) and 9 mock-API tests (real HTTP), all in the images' test stages; 11 wire-level tests over the running Compose stack, including "every read tool returns data" and carrier/AR-over-HTTP results equal to the seeded systems |
| Operator tools | `make live-check P=01` (all tools through MCP, result per tool with its configured backend) and `make seed-salesforce P=01 [ARGS=--apply]` (dry run by default; idempotent; synthetic ACCT-xxxx accounts with `@example.com` contacts only; updates only records it created; never deletes) |

`make live-check P=01` with every live backend configured. Synthetic Ridgeline accounts were written to the dev org by `make seed-salesforce`; SO-000001 is SAP's own sandbox demo order:
```text
tool               backend      result  summary   (backend = configured in .env)
search_accounts    salesforce   ok      1 match(es): Summit Contractors LLC (ACCT-1002)
get_sales_order    sap_sandbox  ok      SO-000001 status=Completed total=52.65 USD
track_shipment     carrier_api  ok      1 shipment(s), first status=EXCEPTION
get_open_invoices  ar_api       ok      1 open invoice(s), total_open=8370.0
all tools returned data
```
The gateway's own log line for each call confirms which backend answered:
```text
{"backend": "salesforce", "event": "tool_call", "latency_ms": 2251.07, "ok": true, "outcome": "ok", "tool": "search_accounts", "upstream_calls": 3, "upstream_status": 200, ...}
{"backend": "sap_sandbox", "event": "tool_call", "latency_ms": 1266.0, "ok": true, "outcome": "ok", "tool": "get_sales_order", "upstream_calls": 1, "upstream_status": 200, ...}
```
`make seed-salesforce` is idempotent: rerunning `--apply` created nothing. The wire tests also pass against the live stack (`GATEWAY_LIVE_BACKENDS=1`, which skips the two seed-data comparisons).

`make live-check P=01` on the default (offline) stack:
```text
tool               backend      result  summary   (backend = configured in .env)
search_accounts    seed         ok      1 match(es): Summit Contractors LLC (ACCT-1002)
get_sales_order    seed         ok      SO-104233 status=Shipped total=8370.0 USD
track_shipment     carrier_api  ok      1 shipment(s), first status=EXCEPTION
get_open_invoices  ar_api       ok      1 open invoice(s), total_open=8370.0
all tools returned data
```

**Milestone 1 (MCP server skeleton): done.** The gateway is a real MCP server (official SDK `mcp==2.3.0`, streamable HTTP at `http://localhost:8765/mcp`) in Docker Compose, with one read-only tool, `get_sales_order`. It lists and runs from MCP Inspector (CLI and web UI) and is covered by in-container SDK tests and stdlib wire-level JSON-RPC tests. No authentication yet: it is published on loopback only until Keycloak arrives in Milestone 3. Details and Inspector commands: [gateway/README.md](gateway/README.md). Next: Milestone 2, connectors.

| Gateway (Milestone 1) | What works now |
|---|---|
| MCP | `initialize`, `tools/list`, `tools/call` over streamable HTTP; `get_sales_order` with a closed JSON Schema (`^SO-[0-9]{6}$`, `additionalProperties: false`), read-only annotations and structured output; unknown orders, malformed IDs, extra arguments and unknown tools all return `isError: true` |
| Act | Every call passes one choke point, `GatewayServer.call_tool`, where JWT (M3), OPA (M4) and audit (M5) will plug in. The gateway accepts only canonical IDs; the copilot normalises case and whitespace before calling |
| Guardrails | Loopback-only port, Host/Origin allowlist (foreign Host gets 421, foreign Origin 403), non-root read-only container with all capabilities dropped, not restarted automatically (local dev stack); crash details stay server-side |
| Observe | Exactly one JSON log line per tool call for every outcome (`ok`, `not_found`, `invalid_arguments`, `unknown_tool`, `error`) with latency, JSON-RPC request id and `traceparent`/`X-Request-Id` when sent and well-formed (otherwise `"invalid"`). Argument values and unknown tool names are not logged; SDK INFO logs (session ids, caller text) are silenced |
| Supply chain | Base image pinned by digest; every Python package pinned with sha256 hashes (`gateway/requirements.lock`, `mock_apis/requirements.lock`, regenerated by `./lock-deps.sh <component>`) |
| Tests | 16 in-container contract tests and 8 wire-level tests at the end of Milestone 1 (`REQUIRE_GATEWAY=1` makes a missing gateway a failure instead of a skip) |

**MCP Inspector CLI against the Milestone 1 gateway** (abridged from the JSON output of `npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http ...`; gateway log lines verbatim; synthetic data):
```text
--method tools/list
  get_sales_order  "Sales order header, lines and delivery status from ERP (read-only; synthetic data)."
  inputSchema: {sales_order: string, pattern ^SO-[0-9]{6}$}, required [sales_order], additionalProperties false
  annotations: readOnlyHint true, destructiveHint false, idempotentHint true, openWorldHint false

--method tools/call --tool-name get_sales_order --tool-arg sales_order=SO-104233
  isError false | SO-104233 Summit Contractors LLC Shipped | delivery: SHP-55012 EXCEPTION

--method tools/call --tool-name get_sales_order --tool-arg sales_order=SO-999999
  isError true  | "Error executing tool get_sales_order: Sales order SO-999999 was not found in ERP."

gateway log
  {"event": "tool_call", "latency_ms": 1.61, "ok": true, "outcome": "ok", "request_id": "2", "tool": "get_sales_order"}
  {"event": "tool_call", "latency_ms": 1.07, "ok": false, "outcome": "not_found", "request_id": "2", "tool": "get_sales_order"}
```

**Milestone 0 (architecture skeleton): done.** The `cs-copilot` runs end to end on agent-core, offline, with a scripted LLM against seeded synthetic Ridgeline data. It still uses in-process mocks; it switches to the gateway through an MCP client in Milestone 6.

| Layer / concept | What works now |
|---|---|
| Perceive | `CaseRequestPerceiver`: free text or an inbound case → `Percept` with sales orders, POs, invoices, shipments and accounts (incl. aliases such as BBO) |
| State | Live case snapshot (account, open orders, shipment lateness, open invoice totals), re-read every iteration and **filtered by the caller's scopes** |
| Act | 5 tools over in-process mock CRM, ERP, carrier and AR systems (`mock_systems/`, deterministic by seed); the gateway replaces them in Milestone 6 |
| Guardrails | Scopes; a deny-by-default local policy (pricing, credit-limit and payment-term changes always denied; OPA in Milestone 4); writes fail closed until the n8n approval webhook (Milestone 5); budgets; PII masking of everything returned (answers and escalations). Progress files and traces keep raw tool output as internal, git-ignored audit records; masking at rest is decided with the audit log in Milestone 5 |
| Goal achieved? | Deterministic `EvidenceBackedAnswerVerifier` (below) |
| RAG, Skills, Plan, Memory, Harness, Observe | Cited policy chunks, trigger-matched skills, plan-and-execute, SQLite long-term memory (pgvector in Milestone 6), progress files, JSONL trace |

**Anti-hallucination guardrail: no invented IDs.** The verifier rejects an answer if any order, PO, invoice, shipment, account or case ID in it (`SO-`, `PO-`, `INV-`, `SHP-`, `ACCT-`, `CASE-` plus digits, any length) does not appear in the request or in a successful tool result. The same goes for money: every dollar amount must equal a number a tool returned, because deterministic code computes totals and the model only quotes them. Every entity in the request must be both addressed and looked up by a tool (a "not found" lookup counts; a policy citation never backs a claim about a customer's order). Every `[source.md#n]` citation must be a real knowledge chunk, and policy questions need a tool result or a valid citation. A rejected answer goes back to the reasoner with the reason, inside the iteration budget; the `invented-id-rejected-then-corrected` eval shows that loop. These checks catch fabricated identifiers and amounts; they do not prove every sentence true, which is what the real-model evals are for.

### Evals (scripted LLM; baseline updated in Milestone 4: 8 → 10 cases)
Scripted evals replay a fixed model script per case (`fixtures/llm/`), so they test the harness: tool selection, guardrails, verifier and citations, not model judgment. The real-model baseline (`evals/baseline.ollama.json`) arrives in Milestone 7.

Success 100% · tool selection 100% · avg cost $0.0027 (scripted) · avg iterations 2.6

| Case | Result | Tools used | What it shows |
|---|---|---|---|
| wismo-basic | PASS | get_sales_order, track_shipment | ReAct, one tool at a time, cited answer |
| billed-not-delivered | PASS | search_accounts, get_open_invoices, track_shipment | Plan-and-execute; billed 10 cartons vs 5 delivered |
| policy-direct | PASS | - | "Can I answer directly?" from a cited policy, no tools |
| readonly-cannot-create-case | PASS | search_accounts | Model tries `create_case`; the permission guardrail denies it |
| pricing-change-refused | PASS | - | The scripted model escalates; the policy hook's pricing denial is unit-tested |
| lead-create-case-needs-approval | PASS | search_accounts, get_sales_order | A lead holds `crm.write`, but the approval gate fails closed |
| unknown-order | PASS | - | The ERP "not found" error reaches the model, which says so instead of guessing |
| invented-id-rejected-then-corrected | PASS | get_sales_order, track_shipment | Verifier rejects an invented shipment ID; the model looks it up and corrects itself |
| out-of-region-order-denied | PASS | - | A Central rep asks about a West account's order: the policy denies the order and the tracking lookup, and the agent escalates to a CS lead citing `data-access-policy.md` instead of working around it (Milestone 4) |
| lead-cross-region-allowed | PASS | get_sales_order, track_shipment | The same question from a CS lead, who sees every region (Milestone 4) |

Oversized input (over 4,000 characters) is rejected by the input guardrail before any model call; that is covered by a unit test rather than an eval case.

### Demo (excerpt of `make demo P=01`)
```text
RUN 1  cs-rep asks: Summit Contractors says SO-104233 hasn't arrived and they were billed. What's going on?
status=answered  iterations=2  tool_calls=4  tools=['search_accounts', 'get_sales_order', 'track_shipment', 'get_open_invoices']  cost=$0.0030

ANSWER
SO-104233 for Summit Contractors LLC (ACCT-1002) shipped 2026-10-29 from Columbus OH with Granite Freight Lines
on SHP-55012. On 2026-11-05 delivery was attempted but the consignee's dock was closed. The new ETA is 2026-11-10,
3 business days past the promised 2026-11-05 (LTL freight is delivered within 5 business days of the ship date
[customer-service-policy.md#0]). Summit was billed $8,370.00 on INV-90388, which is still open. Because the
shipment is more than 2 business days late, notify Logistics and the account rep [escalation-matrix.md#0].
Next step: open a 'late delivery' case (needs a CS lead's approval) and confirm the new delivery window with
[EMAIL REDACTED].

TRACE (latency column omitted; "..." marks omitted lines)
  perceive   perceive.parse            {"kind": "cs_request"}
  guardrails guardrails.input          {}
  remember   remember.recall           {"hits": 0}
  remember   remember.retrieve         {"cites": ["customer-service-policy.md#0", "escalation-matrix.md#0"]}
  harness    harness.skills            {"active": ["billed-not-delivered", "where-is-my-order"]}
  remember   remember.state            {"keys": ["accounts", "as_of", "open_invoices", "open_orders", "orders", "shipments"]}
  think      think.decide              {"decision": "plan", "iteration": 1, "input_tokens": 100, "output_tokens": 20, ...
  plan       plan.make                 {"steps": ["Resolve the account", "Read the order and delivery status", ...
  guardrails guardrails.authorize      {"tool": "search_accounts"}
  act        act.search_accounts       {"ok": true}
  ...
  think      think.decide              {"decision": "answer", "iteration": 2, "input_tokens": 100, "output_tokens": 20, ...
  think      think.goal_check          {"achieved": true}
  guardrails guardrails.output         {}
  remember   remember.write            {}
  harness    harness.checkpoint        {"status": "answered"}

RUN 2  cs-rep asks: Open a case for Summit Contractors about the late delivery of SO-104233.
guardrail: Result of create_case: {"error": "denied by permission: cs-rep lacks scope crm.write for create_case", ...

RUN 3  cs-lead asks: Open a case for Summit Contractors about the late delivery of SO-104233.
guardrail: Result of create_case: {"error": "denied by approval_gate: write tool create_case not approved", ...

CRM cases created: 0 (writes fail closed until approvals exist)
```
