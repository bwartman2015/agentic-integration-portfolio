# Shipping & Logistics: Shipment Exception and Freight Audit Agents

> Part of the [Agentic Integration Portfolio](../../README.md): enterprise AI agents for a fictional company, Ridgeline Supply Co. All data is synthetic.

**Business operation:** Shipping · Logistics

Agents that watch live shipment state, resolve late, damaged and short deliveries before the customer calls, and audit freight invoices against carrier contracts.

## The business problem
Ridgeline ships about 9,000 parcels and 400 LTL loads a week. Roughly 4% go late, arrive damaged or arrive short. Logistics coordinators chase carriers by phone, customers call customer service, and retailers fine Ridgeline for late deliveries and inaccurate ASNs. Freight invoices are overbilled on about 2% of shipments (wrong class, missing discount, duplicate accessorials), and nobody has time to audit them. This project adds a **Shipment Exception Agent** and a **Freight Audit Agent**.

## Architecture
```mermaid
flowchart TB
    IN["Input<br/>Shipping · Logistics events and requests"] --> PER["PERCEIVE<br/>ShipmentExceptionPerceiver, FreightInvoicePerceiver"]
    PER --> CTX["HARNESS: context injection<br/>state + memory + RAG + skills"]
    CTX --> LLM["THINK<br/>shipment-exception-agent, freight-audit-agent"]
    LLM <--> MEM[("REMEMBER<br/>memory + state")]
    RAG[("RAG<br/>knowledge/*.md")] --> CTX
    SK[["SKILLS<br/>skills/*/SKILL.md"]] --> CTX
    LLM --> Q1{"Can I answer<br/>directly?"}
    Q1 -- yes --> OUT["Output"]
    Q1 -- no --> PLAN["PLAN<br/>ordered sub-tasks"]
    PLAN --> ACT
    ACT["ACT via MCP / APIs / code<br/>track_shipment, get_proof_of_delivery, get_shipment_order<br/>get_contract_rate, reroute_or_reschedule, notify_customer<br/>open_freight_claim, dispute_freight_invoice"]
    ACT -- results --> Q2{"Goal achieved?<br/>ExceptionDispositionVerifier, FreightAuditVerifier"}
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
| 1 | Memory & State | **Showcase of State:** the live shipment snapshot is re-read every iteration, so the agent sees its own reroute take effect. Memory: carrier/lane performance and past claims. |
| 2 | Orchestration | Exception events are routed by type to the right specialist (exception agent vs freight audit agent). |
| 3 | RAG | Carrier contracts and customer delivery requirements, cited in every decision. |
| 4 | Harness | Progress files per shipment; compaction for long multi-day exceptions. |
| 5 | Evals | Late, short, damaged, no-action-needed and freight-billing cases, including 'should NOT act' cases. |
| 6 | MCP | Carrier and TMS mocks are exposed as MCP tools through the Project 01 gateway. |
| 7 | Skills | `late-delivery-playbook`, `damaged-or-short-delivery`, `freight-invoice-audit`. |
| 8 | A2A | The exception agent publishes an Agent Card ('shipment-status') used by Project 08. |
| 9 | Multi-Agent System | Exception and freight-audit specialists share evidence (late beyond SLA -> refund eligibility). |

## Run it
```bash
make test P=03          # from the repo root
make arch-check P=03
make eval P=03          # after Milestone 0
```

## Status
Eval results and a demo are added as milestones complete.
