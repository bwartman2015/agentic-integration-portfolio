# Agent architecture: SAP Integration Suite: Order 360 Assistant (orders, deliveries, billing, credit)

```mermaid
flowchart TB
    IN["Input<br/>Customer Service · Credit · SAP events and requests"] --> PER["PERCEIVE<br/>Order360QueryPerceiver"]
    PER --> CTX["HARNESS: context injection<br/>state + memory + RAG + skills"]
    CTX --> LLM["THINK<br/>order360-assistant"]
    LLM <--> MEM[("REMEMBER<br/>memory + state")]
    RAG[("RAG<br/>knowledge/*.md")] --> CTX
    SK[["SKILLS<br/>skills/*/SKILL.md"]] --> CTX
    LLM --> Q1{"Can I answer<br/>directly?"}
    Q1 -- yes --> OUT["Output"]
    Q1 -- no --> PLAN["PLAN<br/>ordered sub-tasks"]
    PLAN --> ACT
    ACT["ACT via MCP / APIs / code<br/>get_order_360, get_credit_exposure"]
    ACT -- results --> Q2{"Goal achieved?<br/>CorrelatedAnswerVerifier"}
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
| `order360-assistant` | specialist | `Order360QueryPerceiver` | `CorrelatedAnswerVerifier` | no | - |

## Agentic AI concepts
| # | Concept | How this project uses it |
|---|---------|--------------------------|
| 1 | Memory & State | Memory: customer aliases. State: the customer's open orders and credit status during the conversation. |
| 2 | Orchestration | CPI multicast orchestrates S/4 + CRM calls; the agent sees one governed tool. |
| 3 | RAG | SAP status glossary retrieved so codes become plain English. |
| 4 | Harness | Correlation id flows from agent trace -> MCP -> API-M -> CPI and back into the answer. |
| 5 | Evals | Delivery, billing, credit-hold and glossary cases. |
| 6 | MCP | `get_order_360` is registered as an MCP tool in the Project 01 gateway. |
| 7 | Skills | `credit-hold-explainer`. |
| 8 | A2A | Publishes an Agent Card ('order-360') used by Project 08. |
| 9 | Multi-Agent System | Serves the Command Center as the SAP specialist. |
