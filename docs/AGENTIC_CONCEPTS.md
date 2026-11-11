# Agentic AI concepts: where each one lives

Every project uses the **seven-layer agent architecture** (`docs/ARCHITECTURE.md`) and the **nine agentic AI concepts** below. The framework code is in `agent-core/agentcore/`; each project applies it to a real business operation at the fictional Ridgeline Supply Co. (`docs/COMPANY.md`).

★ = showcase project for that concept · ✓ = used · · = optional or deliberately not used (the reason is in the project's README)

| Concept | agent-core | 01 | 02 | 03 | 04 | 05 | 06 | 07 | 08 |
|---|---|---|---|---|---|---|---|---|---|
| Memory & State | `memory.py`, `state.py` | ✓ | ✓ | ★ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Orchestration | `orchestrate.py` (`agent_as_tool`, `MANAGER_PROMPT`) | ✓ | ✓ | ✓ | ★ | ★ | ✓ | ✓ | ✓ |
| RAG | `rag.py` (`KeywordRetriever`, citations) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Harness | `loop.py`, `harness.py` (context injection, compaction, checkpoints) | ✓ | ✓ | ✓ | ✓ | ★ | ★ | ✓ | ✓ |
| Evals | `evals.py` (`run_evals`, baseline regression) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| MCP | `toolsource.py` (`register_tool_source`) | ★ | ✓ | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| Skills | `skills.py` (`SkillLibrary`, `load_skill`, draft skills) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| A2A | `a2a.py` (Agent Card, `serve_agent`, `A2AClient`) | ✓ | ✓ | ✓ | ✓ | ✓ | · | ✓ | ★ |
| Multi-Agent System | `a2a.py` + `orchestrate.py` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ★ |

Projects: **01** Enterprise MCP Tool Gateway + Customer Service Copilot · **02** Order Management · **03** Shipping & Logistics · **04** Finance Operations · **05** Integration Operations · **06** Integration Delivery · **07** SAP Integration Suite · **08** Operations Command Center

## Concept details by project
### Memory & State
- **01**: Long-term: answered questions, account aliases (pgvector), recalled only for callers with the same scopes and regions as the one who asked. State: the current case and its open orders, refreshed every iteration.
- **02**: Memory: partner order history and resolved exceptions. State: live ATP and the order's current status each iteration.
- **03**: Showcase of State: the live shipment snapshot is re-read every iteration, so the agent sees its own reroute take effect. Memory: carrier/lane performance and past claims.
- **04**: Memory: vendor and customer patterns. State: period status (no proposals into a closed period) and live queue totals.
- **05**: State: live flow health re-read every iteration to verify fixes. Memory: past incidents and fixes that worked.
- **06**: State: CI stage results for this PR. Memory: which AI tests and review findings humans accepted (learned patterns).
- **07**: Memory: customer aliases. State: the customer's open orders and credit status during the conversation.
- **08**: State: the evolving case (who has answered, what's still open). Memory: past disputes and outcomes.
### Orchestration
- **01**: The gateway is the controlled environment that every manager and micro agent in Projects 02–08 acts through.
- **02**: A deterministic pipeline is the orchestrator: clean orders flow straight through; only exceptions are routed to the triage agent and new partners to the copilot.
- **03**: Exception events are routed by type to the right specialist (exception agent vs freight audit agent).
- **04**: Showcase of Orchestration: the Finance Ops Manager breaks the queue into sub-tasks and delegates to AP and AR micro agents (`agent_as_tool`), each with least-privilege scopes.
- **05**: Showcase: the Incident Commander (manager) delegates to a read-only Diagnoser and an action-scoped Remediator; least privilege per micro agent.
- **06**: The PR Orchestrator delegates to the test-generation and review micro agents; the Jenkins pipeline is the environment.
- **07**: CPI multicast orchestrates S/4 + CRM calls; the agent sees one governed tool.
- **08**: The Command Center is the manager; specialists are remote micro agents.
### RAG
- **01**: Customer service policy and escalation matrix are retrieved and cited (`[customer-service-policy.md#0]`).
- **02**: Partner implementation guides and retail compliance rules, retrieved with citations.
- **03**: Carrier contracts and customer delivery requirements, cited in every decision.
- **04**: AP tolerance and AR deduction policies, cited in every proposal.
- **05**: Runbooks and the flow catalog (business impact) are retrieved and cited.
- **06**: API standards retrieved and cited in review comments.
- **07**: SAP status glossary retrieved so codes become plain English.
- **08**: Dispute playbook retrieved and cited.
### Harness
- **01**: agent-core loop: context injection (policy, skills, memory), compaction for long chats, progress files per conversation, masked at rest.
- **02**: agent-core loop with progress files per order, so an aged exception shows exactly where triage stopped.
- **03**: Progress files per shipment; compaction for long multi-day exceptions.
- **04**: Deterministic matching engines are tools (action); agents add judgment and explanation; progress files make every proposal auditable.
- **05**: Showcase: a long-running loop with compaction, progress files per incident, and a headless coding agent as an action for code fixes.
- **06**: Showcase: A headless coding agent is the THINK provider inside CI, with persistence via git commits (separate `ai-generated` commit) and progress files.
- **07**: Correlation id flows from agent trace -> MCP -> API-M -> CPI and back into the answer.
- **08**: Case progress file persisted after every delegation; the case can be resumed.
### Evals
- **01**: `evals/cases.json`: WISMO, billing dispute, policy questions, permission denials, out-of-scope refusals.
- **02**: Planted exceptions (price, quantity, ship-to) and onboarding cases with expected tools.
- **03**: Late, short, damaged, no-action-needed and freight-billing cases, including 'should NOT act' cases.
- **04**: Tolerance, quantity, duplicate, short-pay and manager-delegation cases; cash must balance to the cent.
- **05**: Chaos scenarios replayed as eval cases, including 'must not auto-fix' security cases.
- **06**: PR scenarios with expected tools, including 'do nothing' docs-only PRs.
- **07**: Delivery, billing, credit-hold and glossary cases.
- **08**: End-to-end dispute scenarios scored on the recorded recommendation and specialists consulted.
### MCP
- **01**: Core of this project. The gateway is an MCP server; the copilot connects through an MCP client (`McpToolSource` via `register_tool_source`).
- **02**: Order tools are also published through the Project 01 gateway for other agents.
- **03**: Carrier and TMS mocks are exposed as MCP tools through the Project 01 gateway.
- **04**: Finance tools are published through the Project 01 gateway with ap.*/ar.* scopes.
- **05**: Ops tools exposed via the Project 01 gateway (ops.* scopes).
- **06**: Optional: GitHub and Jenkins tools behind the Project 01 gateway.
- **07**: `get_order_360` is registered as an MCP tool in the Project 01 gateway.
- **08**: Specialists use MCP tools through the Project 01 gateway; the Command Center uses A2A for agents. This is the MCP vs A2A distinction in one system.
### Skills
- **01**: `where-is-my-order`, `billed-not-delivered` runtime skills, loaded by trigger or `load_skill`.
- **02**: `price-mismatch-resolution`, `quantity-spike-triage`, `x12-850-mapping`.
- **03**: `late-delivery-playbook`, `damaged-or-short-delivery`, `freight-invoice-audit`.
- **04**: `three-way-match-variance`, `short-pay-resolution`.
- **05**: `dlq-transient-recovery`, `schema-drift-fix`.
- **06**: `integration-review-checklist`, `api-test-generation`.
- **07**: `credit-hold-explainer`.
- **08**: `deduction-investigation`.
### A2A
- **01**: The copilot publishes an Agent Card so the Command Center (Project 08) can delegate to it.
- **02**: The triage agent publishes an Agent Card ('order-exceptions') used by Project 08.
- **03**: The exception agent publishes an Agent Card ('shipment-status') used by Project 08.
- **04**: The AR agent publishes an Agent Card ('ar-deductions') used by Project 08.
- **05**: The commander publishes an Agent Card ('integration-health') so Project 08 can ask whether a flow is failing.
- **06**: Not exposed externally (internal engineering agent); documented as a deliberate choice.
- **07**: Publishes an Agent Card ('order-360') used by Project 08.
- **08**: Core of this project: discovery via Agent Cards at `/.well-known/agent-card.json`, tasks and artifacts over JSON-RPC `message/send`, bearer-token auth.
### Multi-Agent System
- **01**: One gateway serves many specialist agents, each with its own identity, scopes and rate limit; every call is audited per agent, and writes wait for a human approval.
- **02**: Two specialists (triage, onboarding) with separate prompts, tools and scopes.
- **03**: Exception and freight-audit specialists share evidence (late beyond SLA -> refund eligibility).
- **04**: Manager + two specialists; segregation of duties enforced by separate identities.
- **05**: Commander + Diagnoser + Remediator collaborate on one incident timeline.
- **06**: Orchestrator + two specialists with different write scopes (tests vs comments).
- **07**: Serves the Command Center as the SAP specialist.
- **08**: Core of this project: five specialists from four business functions collaborating through endpoints.

## MCP vs A2A in one sentence
MCP connects an agent to **tools and data** (Project 01's gateway). A2A connects an agent to **other agents** (Project 08's Command Center). Project 08 uses both: specialists reach systems through MCP, and the Command Center reaches the specialists through A2A.
