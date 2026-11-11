# Agent architecture: Integration Delivery: AI-Augmented CI/CD with Test and Review Agents

```mermaid
flowchart TB
    IN["Input<br/>Engineering · API Lifecycle events and requests"] --> PER["PERCEIVE<br/>PRContextPerceiver"]
    PER --> CTX["HARNESS: context injection<br/>state + memory + RAG + skills"]
    CTX --> LLM["THINK<br/>test-generation, integration-review<br/>pr-orchestrator"]
    LLM <--> MEM[("REMEMBER<br/>memory + state")]
    RAG[("RAG<br/>knowledge/*.md")] --> CTX
    SK[["SKILLS<br/>skills/*/SKILL.md"]] --> CTX
    LLM --> Q1{"Can I answer<br/>directly?"}
    Q1 -- yes --> OUT["Output"]
    Q1 -- no --> PLAN["PLAN<br/>ordered sub-tasks"]
    PLAN --> ORC["ORCHESTRATION<br/>pr-orchestrator delegates to<br/>testgen-agent, review-agent"]
    ORC --> ACT
    ACT["ACT via MCP / APIs / code<br/>read_file, get_spec_diff, write_test_file<br/>run_tests, post_review_comment"]
    ACT -- results --> Q2{"Goal achieved?<br/>TestsPassVerifier, ReviewQualityVerifier<br/>PROrchestratorVerifier"}
    Q2 -- yes --> OUT
    Q2 -- "no, within budget" --> LLM
    GR["GUARDRAILS<br/>scopes, policy, approvals, budgets, PII masking"] -.-> ACT
    GR -.-> OUT
    OBS["OBSERVE<br/>traces, cost, latency, evals"] -.-> LLM
    OBS -.-> ACT
```

## Agents
| Agent | Role | Perceiver | Goal check | Planner | Delegates to |
|-------|------|-----------|-----------|---------|--------------|
| `test-generation` | specialist | `PRContextPerceiver` | `TestsPassVerifier` | yes | - |
| `integration-review` | specialist | `PRContextPerceiver` | `ReviewQualityVerifier` | no | - |
| `pr-orchestrator` | manager (orchestrator) | `PRContextPerceiver` | `PROrchestratorVerifier` | yes | testgen-agent, review-agent |

## Agentic AI concepts
| # | Concept | How this project uses it |
|---|---------|--------------------------|
| 1 | Memory & State | State: CI stage results for this PR. Memory: which AI tests and review findings humans accepted (learned patterns). |
| 2 | Orchestration | The PR Orchestrator delegates to the test-generation and review micro agents; the Jenkins pipeline is the environment. |
| 3 | RAG | API standards retrieved and cited in review comments. |
| 4 | Harness | **Showcase:** A headless coding agent is the THINK provider inside CI, with persistence via git commits (separate `ai-generated` commit) and progress files. |
| 5 | Evals | PR scenarios with expected tools, including 'do nothing' docs-only PRs. |
| 6 | MCP | Optional: GitHub and Jenkins tools behind the Project 01 gateway. |
| 7 | Skills | `integration-review-checklist`, `api-test-generation`. |
| 8 | A2A | Not exposed externally (internal engineering agent); documented as a deliberate choice. |
| 9 | Multi-Agent System | Orchestrator + two specialists with different write scopes (tests vs comments). |
