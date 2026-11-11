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
| 1 | Memory & State | Long-term: answered questions and learned account aliases in Postgres + pgvector, embedded by nomic-embed-text-v1.5 on llama.cpp, recalled only for callers with the same access. State: the current case and its open orders, read through the gateway every iteration. |
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
make up P=01            # from the repo root: create/backfill .env, build + start gateway, Keycloak, OPA,
                        # Postgres + pgvector (audit, memory), n8n (approvals) and the mock APIs (Docker Desktop)
make models P=01        # once: download the embedding model (146 MB, SHA-256 verified) into a Docker volume
make ask P=01 Q="Summit Contractors says SO-104233 hasn't arrived and they were billed. What's going on?"
                        # the copilot in its container: tools through the gateway, memory in pgvector
                        # (ROLE=cs-lead to ask as a lead; Q=--list for the questions the scripted model knows)
make opa-test P=01      # Rego unit tests + format check for policy/, in the pinned OPA image
make gateway-test P=01  # gateway + mock-API tests, inside their images' test stages (real OPA binary)
make copilot-test P=01  # copilot image tests with the real MCP SDK (no network)
make test P=01          # includes wire-level MCP + identity tests while the stack is up (skipped otherwise);
                        # live Salesforce/SAP backends are detected and the seed-only comparisons skipped
make live-check P=01    # call all four tools through MCP as cs-rep (real Keycloak token, real OPA decision)
make token P=01 CLIENT=cs-rep   # print a 5-minute access token, e.g. to paste into MCP Inspector
make db-init P=01       # one time, only with DATABASE_URL set (Neon): create the audit and memory tables there
make db-roles P=01      # then: least-privilege gateway_audit and copilot_memory roles, written into .env
make audit P=01         # the audit report: every call, allowed or denied, with its reason; approvals
                        # (ARGS=--trace=<id> shows one copilot run's calls)
make approvals P=01     # write requests waiting for a human, with their approval-form links
make request-case P=01  # as cs-lead, request a CRM case and wait for your decision in the n8n form
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
| 5 | Guardrails, audit, approvals | Rate limits, PII masking, audit table and `/audit` report; n8n approval for `create_case`, denying on timeout | Done |
| 6 | Copilot on the gateway, long-term memory | The copilot calls the gateway through an MCP client; RAG and skills; Postgres + pgvector memory | Done |
| 7 | Evals and A2A card | Real-model eval baseline (Ollama); publish the copilot's Agent Card | Planned |

**Milestone 6 (copilot on the gateway, long-term memory): done.** The copilot now runs in its own container and reaches every enterprise system only through the gateway, as an MCP client (`McpToolSource`, official SDK). Every tool call and every read for its state snapshot carries the asking role's Keycloak token and the run's trace id, so OPA authorizes it and the audit log can show exactly what one answer cost. Long-term memory is Postgres + pgvector: answered questions and learned account aliases, embedded by nomic-embed-text-v1.5 on a local llama.cpp server, and recalled only for callers with the same access. Details: [docs/agent-architecture.md](docs/agent-architecture.md#the-copilot-on-the-gateway-milestone-6).

The demo question asked twice through the gateway (`make ask P=01 Q="..."`, seed CRM/ERP, memory in Neon):
```text
role=cs-rep  status=answered  iterations=2  tool_calls=4  tools=search_accounts,get_sales_order,track_shipment,get_open_invoices
memory: PgVectorMemory (nomic-embed-text-v1.5)
  recalled nothing (first time this audience asks something like this)

ANSWER
SO-104233 for Summit Contractors LLC (ACCT-1002) shipped 2026-10-29 from Columbus OH with Granite Freight Lines on SHP-55012. ...
Summit was billed $8,370.00 on INV-90388, which is still open. ... confirm the new delivery window with [EMAIL REDACTED].

CITATIONS  customer-service-policy.md#0  escalation-matrix.md#0

TOOL CALLS through the gateway (each authorized by OPA and audited; state reads are too)
  search_accounts    ok=True    352.2 ms
  get_sales_order    ok=True    275.9 ms
  track_shipment     ok=True    331.8 ms
  get_open_invoices  ok=True    319.9 ms

trace id 348089d1fa2c41c8b6281b014a991d65
  its gateway audit rows: make audit P=01 ARGS=--trace=348089d1fa2c41c8b6281b014a991d65

# second run, same question
memory: PgVectorMemory (nomic-embed-text-v1.5)
  recalled episode (similarity 0.95, from trace 348089d1fa2c): Q: Summit Contractors says SO-104233 hasn't arrived ...
```
`make audit P=01 ARGS=--trace=348089d1fa2c41c8b6281b014a991d65` lists the 12 gateway calls of the first run (4 tools plus the state reads of both iterations), all `ok` for `cs-rep`, each with that trace id.

| Copilot on the gateway (Milestone 6) | What works now |
|---|---|
| MCP | `McpToolSource` implements agent-core's `ToolSource`; `register_tool_source` turns the gateway's listing into governed tools. Scopes and side effects come from the copilot's own `policy/data.json`: a server that called `create_case` harmless would still get a write that needs `crm.write`. The gateway lists only the tools a token may use, so a rep's copilot has no `create_case` at all |
| State | The snapshot injected into the prompt is read through the gateway with the caller's token, once per record per iteration. A record outside the caller's region appears as "access denied", without contents |
| Guardrails | The copilot still checks scope and restricted changes before calling; the region rule is the gateway's, per record. A lead's `create_case` goes to the gateway's n8n approval and waits for a person; the answer reports the real outcome (the CRM's case id, or the gateway's reason for refusing). Any other write is refused by the copilot |
| Memory | `PgVectorMemory` on `memory.items` (`vector(768)`, HNSW cosine index), in the same database as the audit log: the local `db` container by default, Neon when `MEMORY_DATABASE_URL` is set. Text is PII-masked before it is stored. Recall filters on the caller's audience (scopes + regions) and the embedding model in SQL, so a lead's answers never reach a rep's prompt and vectors of different models are never compared. That separation is the copilot's query, not the database: its one role can read every audience's rows (per-audience row-level security would be the next step for a shared deployment). An episode is found by its question. If the database or the embedder fails, the run answers without memory |
| Memory (aliases) | A search that found exactly one account under another name ("bbo" → Big Box Outdoor) is remembered as an alias for that audience, through a new `on_answer` hook in agent-core |
| Embeddings | llama.cpp server (b12282, CPU, digest-pinned) with nomic-embed-text-v1.5 Q8_0 (`search_query:` / `search_document:` prefixes), on a network only the copilot shares. `make models` downloads the model from a pinned revision and checks its SHA-256. Offline runs use a deterministic hashing embedder |
| Least privilege | Two database roles, locally and in Neon (`make db-roles`): `gateway_audit` (audit tables only) and `copilot_memory` (INSERT and SELECT on `memory.items` only, no temporary tables). Checked live in both databases: `copilot_memory` is refused UPDATE, DELETE, TRUNCATE, DROP, ALTER, CREATE and the audit tables; `gateway_audit` is refused the memory schema. Local-demo limit: the copilot container holds both the cs-rep and cs-lead client secrets, so `ROLE=` picks whose token it uses; in a real deployment the token would come from the signed-in person |
| Observe | agent-core exposes the current trace id; the copilot sends it as `x-request-id` and the gateway stores it on each audit row. `make audit ARGS=--trace=<id>` filters by it |
| State (cost) | The snapshot is re-read through the gateway every iteration, as the State layer requires (one call per record per iteration). These reads are authorized and audited by the gateway but are not agent tool calls: they don't count toward the agent's tool budget. The demo question makes 12 gateway calls for 4 tool calls, well inside the 120-per-minute rate limit |
| Evals | The scripted eval suite (13 cases, unchanged baseline) stays on the offline backend: it measures the harness deterministically. The gateway path is covered by tests on a fake gateway that applies the region rule and lists tools by scope (approved, rejected, a rep refused, out-of-region refusal, recall, aliases) and by `make ask` against the live stack. Milestone 7 adds the real-model baseline |
| Think | Milestone 7 chooses the local chat model. Until then `make ask` replays scripted plans for known questions (`Q=--list`); everything else in the run is live. A script step can branch on a tool's real result, so a case request's answer follows the human's actual decision |
| Container | `copilot/Dockerfile`: digest-pinned python:3.12-slim, hashed lock (`mcp`, `httpx2`, `asyncpg`), agent-core added as a named build context so the repository root never enters the build, non-root, read-only, all capabilities dropped |

Verified (Milestone 6):
- `make ask` (above) on the seeded stack with memory in Neon: answered with citations through the gateway; the second run recalls the first (similarity 0.95); the same question as `cs-lead` recalls nothing (another audience); a different question ("Where is order SO-104233 for Summit Contractors?") recalls the earlier episode at 0.82. A lead's case request with nobody approving was refused by the gateway after `APPROVAL_TIMEOUT_S` and nothing was written.
- `make test`: 120 project tests, 1 skipped (34 new: the copilot on a fake gateway that applies the region rule, pgvector memory on a fake Postgres that evaluates the real queries, embeddings, token handling, alias learning, `make ask`, `make db-roles`) and 38 agent-core tests (trace context, `on_answer`, store-side audience filtering).
- `make copilot-test`: 6 tests with the real MCP SDK (an in-process server, and the streamable HTTP request headers). `make gateway-test`: 177 gateway + 9 mock-API tests. `make opa-test`: 21/21. `make eval P=01`: 13/13, no regression.

**Milestone 5 (guardrails, audit, approvals): done.** Agents propose; a human approves. The gateway now has its first write tool, `create_case`, and it runs only after a person approves it in an n8n workflow. Every authenticated tool call, allowed, denied or cancelled, is written to an audit table with its reason (a request without a valid token is refused before it is a tool call, and is logged). Contact details are masked for callers without the PII scope, and every client has a rate limit. Details: [gateway/README.md](gateway/README.md#guardrails-audit-and-approvals).

How a lead's `create_case` runs:
1. The usual checks first: rate limit, scope (`crm.write`), restricted changes (now checked at any depth, so `{"fields": {"credit_limit": 1}}` is refused), and the account's region.
2. The gateway records an approval request in Postgres. No record, no request: the write is refused.
3. It sends the request to the n8n workflow, with a shared secret n8n checks. n8n answers with a signed link to an approval form and waits, at most as long as the gateway does.
4. A person opens the form (`make approvals P=01` lists the links), chooses Approve or Reject and gives their name.
5. n8n reports the decision to the gateway with a one-time token that only that request's workflow run was given. Only an explicit Approve from a named person creates the case.

Who approves: anyone who opens the signed link. The form itself has no login, and the approver's name is what they type, so the link is handled like a credential. n8n runs on a network only the gateway shares, its editor needs the owner account, and its public API is off.

Anything else is refused and nothing is written: a rejection, no answer within `APPROVAL_TIMEOUT_S` (120 s by default), n8n unreachable or answering anything unexpected, or a decision the audit store cannot record. The approval link is the approver's credential, so it never reaches an agent: no tool returns it, it is not logged or stored, and only an operator token that no agent holds can list it.

| Guardrails, audit, approvals (Milestone 5) | What works now |
|---|---|
| Approvals | `create_case` waits for a human decision in n8n (`n8n/ridgeline-approval.json`, imported at every start). Late, repeated or forged callbacks change nothing; the form expires when the gateway stops waiting. An approval that cannot be recorded does not count |
| Audit | Postgres: the local container on an internal network only the gateway joins, or a hosted database such as Neon when `DATABASE_URL` is set (TLS required). One row per authenticated tool call, including cancelled ones: client, scopes, regions, tool, outcome, reason code, masked argument values, latency, backend, request and trace ids, approval id. Triggers refuse changing or deleting audit rows for every role, and the gateway's own role (`gateway_audit`, locally and in Neon) can only add and read them. At startup the gateway closes any approval a previous process left pending as `unavailable`. A read that cannot be audited is not returned. `GET /audit` (scope `audit.read`, held only by the operator identity `p01-auditor`, never by an agent) and `make audit P=01` give totals by outcome and reason, recent calls and approvals |
| PII masking | Contact emails, phone numbers and SSNs are masked in results unless the token holds `crm.pii` (cs-lead only). JSON numbers and record IDs (`SO-`, `ACCT-`, `INV-` ...) are untouched; a 7-digit number is masked only in the `555-0142` form. Argument values are masked in the audit table for everyone, and the copilot uses the same patterns (a test checks they agree) |
| Rate limits | A sliding one-minute window per verified client (`RATE_LIMIT_PER_MINUTE`, default 120), checked before policy or any backend. Over the limit: a clear tool error, audited as `rate_limited` |
| One list of reason codes | `policy/data.json` holds every reason code. The Rego, the gateway and the copilot read that list, and tests on all three sides fail if a code is missing from it or has no explanation for the caller |
| Masking at rest | The copilot's progress files and traces get the same PII mask as its answers, through an opt-in redaction hook in agent-core's checkpointer and trace sink. Record IDs stay readable |
| Copilot | Its approval hook denies every write unless an approver is installed; none is by default. The copilot reaches the gateway's n8n approval in Milestone 6, when it calls the gateway's tools |

Excerpt of `make audit P=01` after the wire tests:
```text
audit log: 38 call(s)
by outcome: denied=14, invalid_arguments=1, not_found=1, ok=20, unknown_tool=2
by reason:  approval_denied=1, missing_scope=9, out_of_region=2, rate_limited=2

time (UTC)           client          tool               outcome            reason            args
2026-11-11T14:39:35  cs-rep          get_sales_order    not_found          -                 {"sales_order": "SO-999999"}
2026-11-11T14:39:32  cs-lead         create_case        denied             approval_denied   {"account_id": "ACCT-1002", ...}
2026-11-11T14:39:29  p01-rate-test   search_accounts    denied             rate_limited      {"query": "Summit"}
...
requested (UTC)      request                client    account    status     approver
2026-11-11T14:39:30  apr-c8768cde4ec88428   cs-lead   ACCT-1002  rejected   Dana Lead
2026-11-11T14:39:24  apr-6d74ba2894bf5e0f   cs-lead   ACCT-1002  approved   Dana Lead
```

Verified (Milestone 5):
- `make gateway-test`: 177 gateway tests (real OPA binary) + 9 mock-API tests. They cover an audit row for every outcome (including a cancelled call), audit down, rate limits, PII masking, and every approval path, including a decision on a request another process already closed, an unsigned form link, and the webhook secret: approved, rejected, timed out, n8n down or answering nonsense, a look-alike form host, wrong or replayed tokens, a decision that cannot be recorded, and policy refusing before a human is asked.
- `make opa-test`: 21/21 Rego tests, including nested restricted argument names and the shared reason-code list.
- `REQUIRE_GATEWAY=1 make test P=01` on the default stack: 86 tests, none skipped, with real Keycloak, OPA, Postgres and n8n. The approval tests act as the human: they open the real n8n form and submit Approve or Reject.
- The same command on the live stack (Salesforce + SAP sandbox, audit log in Neon as `gateway_audit`): 86 tests, 3 seed-only comparisons skipped. `make live-check`: all four read tools return data.
- `make eval P=01`: 13/13, baseline updated.

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

**Tradeoff: "access denied" discloses that a record exists.** A rep who asks for an order outside their region learns that the order is real. That is deliberate here: reps are internal users, the message tells them who can help, and the copilot can hand the request to a CS lead instead of telling a customer the order does not exist. A gateway serving external callers should answer "not found" instead. A record that does not exist is still "not found" for everyone. Search goes one step further: `hidden_out_of_region` counts the matches a rep may not see, so a rep can learn that an account with a given name exists in another region. That is accepted for internal reps for the same reason, and the per-client rate limits (Milestone 5) bound how fast anyone can probe it.

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
| Guardrails | Scopes; a deny-by-default local policy (pricing, credit-limit and payment-term changes always denied; OPA in Milestone 4); writes run only with an explicit human approval (none is installed by default); budgets; PII masking of everything returned (answers and escalations) and, since Milestone 5, of progress files and traces at rest |
| Goal achieved? | Deterministic `EvidenceBackedAnswerVerifier` (below) |
| RAG, Skills, Plan, Memory, Harness, Observe | Cited policy chunks, trigger-matched skills, plan-and-execute, SQLite long-term memory (pgvector in Milestone 6), progress files, JSONL trace |

**Anti-hallucination guardrail: no invented IDs.** The verifier rejects an answer if any order, PO, invoice, shipment, account or case ID in it (`SO-`, `PO-`, `INV-`, `SHP-`, `ACCT-`, `CASE-` plus digits, any length) does not appear in the request or in a successful tool result. The same goes for money: every dollar amount must equal a number a tool returned, because deterministic code computes totals and the model only quotes them. Every entity in the request must be both addressed and looked up by a tool (a "not found" lookup counts; a policy citation never backs a claim about a customer's order). Every `[source.md#n]` citation must be a real knowledge chunk, and policy questions need a tool result or a valid citation. A rejected answer goes back to the reasoner with the reason, inside the iteration budget; the `invented-id-rejected-then-corrected` eval shows that loop. These checks catch fabricated identifiers and amounts; they do not prove every sentence true, which is what the real-model evals are for.

### Evals (scripted LLM; baseline updated in Milestone 5: 10 → 13 cases)
Scripted evals replay a fixed model script per case (`fixtures/llm/`), so they test the harness: tool selection, guardrails, verifier and citations, not model judgment. The real-model baseline (`evals/baseline.ollama.json`) arrives in Milestone 7.

Success 100% · tool selection 100% · avg cost $0.0028 (scripted) · avg iterations 2.69

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
| lead-create-case-approved | PASS | search_accounts, create_case | A human approves, so the case is created (Milestone 5) |
| lead-create-case-rejected | PASS | search_accounts | A human rejects: nothing is written and the agent says so (Milestone 5) |
| lead-create-case-approval-timeout | PASS | search_accounts | No human answers in time: the approval gate fails closed and the agent reports it without retrying (Milestone 5) |

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
