# Agent architecture: Enterprise MCP Tool Gateway + Customer Service Copilot

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

## Agents
| Agent | Role | Perceiver | Goal check | Planner | Delegates to |
|-------|------|-----------|-----------|---------|--------------|
| `cs-copilot` | specialist | `CaseRequestPerceiver` | `EvidenceBackedAnswerVerifier` | yes | - |

## Agentic AI concepts
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

## The gateway (the environment every agent acts through)
The copilot's ACT layer will reach enterprise systems only through this MCP server. Build-out by milestone: **M1** server skeleton (done), **M2** connectors (done; live check passed against Salesforce and the SAP sandbox), **M3** Keycloak identity (done), M4 OPA, M5 rate limits + PII masking + audit + approvals, M6 the copilot switches to `McpToolSource`.

```mermaid
flowchart LR
    subgraph Clients["MCP clients"]
        INS["MCP Inspector<br/>(CLI / web UI)"]
        CP["cs-copilot ACT layer<br/>McpToolSource (M6)"]
        OTH["Agents in Projects 02-08"]
    end
    subgraph GW["ridgeline-gateway container (127.0.0.1:8765)"]
        AUTHN["IDENTITY (M3)<br/>Bearer JWT required on /mcp<br/>RS256 · iss · aud · exp → else 401"]
        TS["Transport security<br/>Host/Origin allowlist (M1)<br/>OPA (M4) · rate limit (M5)"]
        MCPS["MCPServer, streamable HTTP /mcp<br/>tools/list · tools/call"]
        CT["GatewayServer.call_tool<br/>unknown tool / extra args rejected<br/>logs verified client_id + scopes<br/>OPA (M4) · audit (M5) plug in here"]
        TOOLS["4 read-only tools (M2)<br/>search_accounts · get_sales_order<br/>track_shipment · get_open_invoices"]
        CONN["Connectors (M2)<br/>host-pinned, no redirects, timeouts,<br/>sanitized errors, secrets from env"]
        LOG["OBSERVE: one JSON line per call<br/>backend · upstream status/latency<br/>audit table (M5)"]
    end
    SEED[("seed backends<br/>mock_systems (offline)")]
    SF[("Salesforce Dev Edition<br/>OAuth client credentials")]
    SAP[("SAP sandbox<br/>API_SALES_ORDER_SRV, APIKey")]
    CAR[("mock carrier API<br/>X-API-Key")]
    AR[("mock AR API<br/>Bearer")]
    KC[("Keycloak (M3)<br/>realm ridgeline<br/>clients cs-rep, cs-lead")]
    INS -- "token" --> KC
    CP -. "client credentials" .-> KC
    INS --> AUTHN
    CP -.-> AUTHN
    OTH -.-> AUTHN
    AUTHN -. "signing keys (JWKS)" .-> KC
    AUTHN --> TS --> MCPS --> CT --> TOOLS --> CONN
    CT -.-> LOG
    CONN --> SEED
    CONN -. "CRM_BACKEND=salesforce" .-> SF
    CONN -. "ERP_BACKEND=sap_sandbox" .-> SAP
    CONN --> CAR
    CONN --> AR
```

| Gateway concern | Now (Milestone 2) | Code |
|---|---|---|
| MCP (concept 6) | Server side: `initialize`, `tools/list`, `tools/call` over streamable HTTP; four read-only tools with the copilot's names, arguments and shapes | `gateway/ridgeline_gateway/server.py` |
| Act | Tools call connectors, the only code that talks to backends. Each system has a `seed` backend and a live one (Salesforce, SAP sandbox, mock carrier/AR APIs), chosen in `.env`. Deterministic code computes AR totals. The gateway enforces canonical IDs; the copilot normalises case/whitespace | `connectors/`, `mock_apis/` |
| Guardrails | Closed JSON Schemas; secrets only from env, masked, least privilege per container; fail-closed startup; read-only (GET) connectors pinned to host and port, with no redirects, size caps and timeouts; mock APIs on an internal-only network; Salesforce `instance_url` validated before the token is sent; sanitized upstream errors; loopback-only port; DNS-rebinding protection; hardened containers; digest- and hash-pinned images. **No identity yet (M3)** | `config.py`, `connectors/base.py`, `docker-compose.yml`, Dockerfiles, lock files |
| Observe | One JSON line per call for every outcome, with verified `client_id`/`scopes`, backend, upstream status and latency, request id and trace headers; `auth_rejected` lines give the reason; no argument values, secrets or tokens | `server.py` `_log_call`, `auth.py`, `connectors/base.py` |
| Identity (Guardrails, M3) | Keycloak realm `ridgeline`: one confidential client per agent (`cs-rep`, `cs-lead`), service-account grant only, scopes matching the copilot's tools. The gateway verifies RS256 JWTs (exact issuer, audience `ridgeline-gateway`, expiry) and fails closed with 401 + an RFC 9728 challenge; per-tool authorization follows in M4 (OPA) | `auth.py`, `keycloak/ridgeline-realm.json`, `docker-compose.yml` |
| Multi-Agent (concept 9) | Each agent has its own identity and scopes; the gateway logs which agent made each call | same |
| Memory & State (concept 1, partial) | Account aliases (`BBO` for Big Box Outdoor) are searchable in the CRM (SOSL in Salesforce); long-term memory itself is M6 | `connectors/salesforce.py` |
| Tests | Config, connector (fake transports) and tool-contract tests in the gateway image; mock-API tests over real HTTP; stdlib wire-level tests over the running stack; `make live-check` for the live sandboxes | `gateway/tests/`, `mock_apis/tests/`, `tests/test_gateway_http.py`, `admin/live_check.py` |
