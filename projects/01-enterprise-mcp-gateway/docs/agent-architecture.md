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
The copilot's ACT layer will reach enterprise systems only through this MCP server. Build-out by milestone: **M1** server skeleton (done), M2 connectors, M3 Keycloak JWT, M4 OPA, M5 rate limits + PII masking + audit + approvals, M6 the copilot switches to `McpToolSource`.

```mermaid
flowchart LR
    subgraph Clients["MCP clients"]
        INS["MCP Inspector<br/>(CLI / web UI)"]
        CP["cs-copilot ACT layer<br/>McpToolSource (M6)"]
        OTH["Agents in Projects 02-08"]
    end
    subgraph GW["ridgeline-gateway container (127.0.0.1:8765)"]
        TS["Transport security<br/>Host/Origin allowlist (M1)<br/>JWT (M3) · OPA (M4) · rate limit (M5)"]
        MCPS["MCPServer, streamable HTTP /mcp<br/>tools/list · tools/call"]
        CT["GatewayServer.call_tool (M1)<br/>unknown tool / extra args rejected<br/>JWT (M3) · OPA (M4) · audit (M5) plug in here"]
        T1["get_sales_order<br/>read-only, closed schema (M1)"]
        LOG["OBSERVE: one JSON line per call, every outcome (M1)<br/>audit table (M5)"]
    end
    ERP[("mock ERP, seeded<br/>SAP sandbox in M2")]
    INS --> TS
    CP -.-> TS
    OTH -.-> TS
    TS --> MCPS --> CT --> T1 --> ERP
    CT -.-> LOG
```

| Gateway concern | Milestone 1 | Code |
|---|---|---|
| MCP (concept 6) | Server side: `initialize`, `tools/list`, `tools/call` over streamable HTTP | `gateway/ridgeline_gateway/server.py` |
| Act | One tool, `get_sales_order`: same name and parameter as the copilot's tool. The gateway enforces `^SO-[0-9]{6}$`; the copilot normalises case/whitespace before calling. Every call passes `GatewayServer.call_tool` | same |
| Guardrails | Closed JSON Schema (`additionalProperties: false`, extra arguments rejected); loopback-only port; DNS-rebinding protection; hardened container; digest- and hash-pinned image. **No identity yet (M3)** | `server.py`, `docker-compose.yml`, `gateway/Dockerfile`, `gateway/requirements.lock` |
| Observe | One JSON line per call for every outcome, with request id and trace headers when present; no argument values | `server.py` `_log_call` |
| Tests | SDK contract tests in the container; stdlib wire-level JSON-RPC tests against the running container | `gateway/tests/`, `tests/test_gateway_http.py` |
