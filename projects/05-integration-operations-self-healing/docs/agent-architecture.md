# Agent architecture: Integration Operations: Self-Healing Incident Commander

```mermaid
flowchart TB
    IN["Input<br/>IT Operations · Integration Platform events and requests"] --> PER["PERCEIVE<br/>IncidentPerceiver"]
    PER --> CTX["HARNESS: context injection<br/>state + memory + RAG + skills"]
    CTX --> LLM["THINK<br/>diagnoser, remediator<br/>incident-commander"]
    LLM <--> MEM[("REMEMBER<br/>memory + state")]
    RAG[("RAG<br/>knowledge/*.md")] --> CTX
    SK[["SKILLS<br/>skills/*/SKILL.md"]] --> CTX
    LLM --> Q1{"Can I answer<br/>directly?"}
    Q1 -- yes --> OUT["Output"]
    Q1 -- no --> PLAN["PLAN<br/>ordered sub-tasks"]
    PLAN --> ORC["ORCHESTRATION<br/>incident-commander delegates to<br/>diagnoser, remediator"]
    ORC --> ACT
    ACT["ACT via MCP / APIs / code<br/>get_traces, sample_dlq, get_metrics<br/>replay_dlq, pause_consumer, set_rate_limit<br/>quarantine, open_fix_pr, notify_oncall"]
    ACT -- results --> Q2{"Goal achieved?<br/>DiagnosisVerifier, MetricsBaselineVerifier<br/>CommanderVerifier"}
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
| `diagnoser` | specialist | `IncidentPerceiver` | `DiagnosisVerifier` | no | - |
| `remediator` | specialist | `IncidentPerceiver` | `MetricsBaselineVerifier` | yes | - |
| `incident-commander` | manager (orchestrator) | `IncidentPerceiver` | `CommanderVerifier` | yes | diagnoser, remediator |

## Agentic AI concepts
| # | Concept | How this project uses it |
|---|---------|--------------------------|
| 1 | Memory & State | State: live flow health re-read every iteration to verify fixes. Memory: past incidents and fixes that worked. |
| 2 | Orchestration | **Showcase:** the Incident Commander (manager) delegates to a read-only Diagnoser and an action-scoped Remediator; least privilege per micro agent. |
| 3 | RAG | Runbooks and the flow catalog (business impact) are retrieved and cited. |
| 4 | Harness | **Showcase:** a long-running loop with compaction, progress files per incident, and Claude Code headless as an action for code fixes. |
| 5 | Evals | Chaos scenarios replayed as eval cases, including 'must not auto-fix' security cases. |
| 6 | MCP | Ops tools exposed via the Project 01 gateway (ops.* scopes). |
| 7 | Skills | `dlq-transient-recovery`, `schema-drift-fix`. |
| 8 | A2A | The commander publishes an Agent Card ('integration-health') so Project 08 can ask whether a flow is failing. |
| 9 | Multi-Agent System | Commander + Diagnoser + Remediator collaborate on one incident timeline. |
