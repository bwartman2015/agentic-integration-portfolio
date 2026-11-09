# Agent architecture: Finance Operations: AP Exception and AR Cash Application Agents

```mermaid
flowchart TB
    IN["Input<br/>Finance · Accounts Payable · Accounts Receivable events and requests"] --> PER["PERCEIVE<br/>FinanceRequestPerceiver"]
    PER --> CTX["HARNESS: context injection<br/>state + memory + RAG + skills"]
    CTX --> LLM["THINK<br/>ap-exception-agent, ar-cash-application-agent<br/>finance-ops-manager"]
    LLM <--> MEM[("REMEMBER<br/>memory + state")]
    RAG[("RAG<br/>knowledge/*.md")] --> CTX
    SK[["SKILLS<br/>skills/*/SKILL.md"]] --> CTX
    LLM --> Q1{"Can I answer<br/>directly?"}
    Q1 -- yes --> OUT["Output"]
    Q1 -- no --> PLAN["PLAN<br/>ordered sub-tasks"]
    PLAN --> ORC["ORCHESTRATION<br/>finance-ops-manager delegates to<br/>ap-agent, ar-agent"]
    ORC --> ACT
    ACT["ACT via MCP / APIs / code<br/>get_purchase_order, get_goods_receipts, get_supplier_invoice<br/>three_way_match, check_duplicate_invoice, propose_ap_resolution<br/>get_remittance, get_open_items, match_remittance<br/>propose_cash_application"]
    ACT -- results --> Q2{"Goal achieved?<br/>APResolutionVerifier, CashApplicationVerifier<br/>ManagerVerifier"}
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
| `ap-exception-agent` | specialist | `FinanceRequestPerceiver` | `APResolutionVerifier` | no | - |
| `ar-cash-application-agent` | specialist | `FinanceRequestPerceiver` | `CashApplicationVerifier` | no | - |
| `finance-ops-manager` | manager (orchestrator) | `FinanceRequestPerceiver` | `ManagerVerifier` | yes | ap-agent, ar-agent |

## Agentic AI concepts
| # | Concept | How this project uses it |
|---|---------|--------------------------|
| 1 | Memory & State | Memory: vendor and customer patterns. State: period status (no proposals into a closed period) and live queue totals. |
| 2 | Orchestration | **Showcase of Orchestration:** the Finance Ops Manager breaks the queue into sub-tasks and delegates to AP and AR micro agents (`agent_as_tool`), each with least-privilege scopes. |
| 3 | RAG | AP tolerance and AR deduction policies, cited in every proposal. |
| 4 | Harness | Deterministic matching engines are tools (action); agents add judgment and explanation; progress files make every proposal auditable. |
| 5 | Evals | Tolerance, quantity, duplicate, short-pay and manager-delegation cases; cash must balance to the cent. |
| 6 | MCP | Finance tools are published through the Project 01 gateway with ap.*/ar.* scopes. |
| 7 | Skills | `three-way-match-variance`, `short-pay-resolution`. |
| 8 | A2A | The AR agent publishes an Agent Card ('ar-deductions') used by Project 08. |
| 9 | Multi-Agent System | Manager + two specialists; segregation of duties enforced by separate identities. |
