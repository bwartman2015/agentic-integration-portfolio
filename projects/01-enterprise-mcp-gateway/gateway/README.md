# Gateway (MCP server)

The Ridgeline enterprise MCP gateway is the one controlled **environment** that every agent in the portfolio acts through. It is built on the official MCP Python SDK (`mcp==2.3.0`, `MCPServer`, which was called FastMCP before SDK 2.x) and serves **streamable HTTP** at `/mcp`. All data is synthetic (fictional Ridgeline Supply Co.).

## Status: Milestone 2 (connectors). Offline parts done; live sandbox check pending credentials
| | Now | Arrives in |
|---|---|---|
| Tools | Four read-only tools: `search_accounts`, `get_sales_order`, `track_shipment`, `get_open_invoices`. They keep the copilot's names, arguments and output shapes. | M5: `create_case` (write) behind the n8n approval |
| Connectors | One per system, each with a `seed` backend (offline synthetic data) and a live backend, chosen in `.env`. The table below lists them. | |
| Choke point | Every `tools/call` goes through `GatewayServer.call_tool`. It rejects unknown tools and unexpected arguments and logs every outcome. | M3 JWT, M4 OPA and M5 audit plug in here |
| Input validation | Closed JSON Schemas (`additionalProperties: false`):<br>• `sales_order` must match `^SO-[0-9]{6}$`<br>• `account_id` must match `^ACCT-[0-9]{4}$`<br>• `query` must match `^[A-Za-z0-9][A-Za-z0-9 &.,'-]{1,79}$`<br>The gateway accepts only canonical IDs; clients normalise case and whitespace. | |
| Secrets | Read only from the environment and masked in every repr. Each container gets only its own variables. A half-configured live backend stops startup (fail closed). | |
| Writes | None from the gateway. Its connectors may only send `GET` (plus the Salesforce token `POST`), enforced in `HttpConnector`. The operator seed tool alone builds a `POST`/`PATCH` Salesforce connector, and it never sends `DELETE` or `PUT`. | M5: `create_case` behind approval |
| Transport security | Published on `127.0.0.1` only. Host and Origin allowlist (a bad Host gets 421, a bad Origin 403). The containers are non-root and read-only, with all capabilities dropped and `restart: "no"`. The mock APIs sit on an internal-only Docker network (`internal: true`: no route to the host or the internet). Images are local builds only (`pull_policy`). | |
| Identity | **None yet. Do not expose this port beyond localhost.** | M3: Keycloak JWT validation; consider `stateless_http=True` with per-request JWTs |
| Authorization | None yet | M4: OPA per-tool scopes and region data rule |
| Observability | One JSON line per call: `{"event": "tool_call", "tool", "outcome", "ok", "latency_ms", "backend", "upstream_status"?, "upstream_ms"?, "upstream_calls"?, "request_id", "traceparent"?, "x_request_id"?, "error_type"?}`.<br>• Outcomes: `ok`, `not_found`, `invalid_arguments`, `unknown_tool`, `upstream_error`, `error`.<br>• Not logged: argument values, unknown tool names, malformed trace headers. SDK and HTTP-client INFO logs are silenced.<br>• Clients see only `Error executing tool <name>` for crashes and a sanitized message for upstream failures. | M5: audit table, rate limits, PII masking, approvals |

### Connectors
| Tool | System | `seed` | Live backend (`.env`) | Auth | Mapping notes |
|---|---|---|---|---|---|
| `search_accounts` | CRM | `mock_systems` | `CRM_BACKEND=salesforce`: Developer Edition REST API | OAuth 2.0 client credentials against the My Domain URL (`*.my.salesforce.com`, https, default port). Token cached 15 min, refreshed once on 401 (never on 403). The returned `instance_url` must also be a My Domain host before the token is sent there. | Standard fields only: `AccountNumber`=ACCT-xxxx, `Site`=region, `Description` lines `Type:` and `Aliases:`, first Contact = primary contact. SOQL (name/number) plus SOSL (aliases), both escaped. |
| `get_sales_order` | ERP | `mock_systems` | `ERP_BACKEND=sap_sandbox`: SAP Business Accelerator Hub `API_SALES_ORDER_SRV` (OData v2) | `APIKey` header | SAP's demo data, not Ridgeline's. SAP `1` maps to `SO-000001`; orders above 999999 can't be addressed. Same keys as a seed order, `null` where SAP has no equivalent (`account_id`, `account_name`, `ship_from`, `service`). `RequestedDeliveryDate` maps to `promised_delivery`; `unit_price` comes from `NetPriceAmount` only when the price is per 1 unit. SAP-only fields go under `source`. |
| `track_shipment` | Carrier | `mock_systems` | `CARRIER_BACKEND=http`: `mock_apis` carrier service (Compose default) | `X-API-Key` | Same shape as the seed backend |
| `get_open_invoices` | AR | `mock_systems` | `AR_BACKEND=http`: `mock_apis` AR service (Compose default) | `Authorization: Bearer` | Every item is validated (dict, same account, numeric amount). The gateway computes `count` and `total_open` in `Decimal` |

Every connector request is pinned to its host and port, never follows redirects, uses `HTTP_TIMEOUT_S` (default 10) and streams bodies, stopping as soon as one passes 5 MB (never buffered whole). Connection pools close on shutdown. Errors map to sanitized `ConnectorError` messages:
- credentials rejected (401)
- refused (403)
- rate limited (429)
- HTTP status
- unreachable
- timed out
- too large
- unreadable or unexpected payload

Live sandbox setup (Salesforce External Client App, SAP API key): [../docs/live-sandbox-setup.md](../docs/live-sandbox-setup.md).

## Layout
```
gateway/
  ridgeline_gateway/server.py        GatewayServer (MCPServer subclass), the four tools, /healthz, startup
  ridgeline_gateway/config.py        settings from env, Secret, fail-closed validation
  ridgeline_gateway/connectors/      base (HttpConnector, errors, upstream info), seed, salesforce, sap, http_mocks
  ridgeline_gateway/admin/           live_check (all tools through MCP), seed_salesforce (synthetic accounts)
  tests/                             config, connectors (httpx2.MockTransport), tool contracts + logging,
                                     admin tools (seed_salesforce against a fake org, live_check)
  pyproject.toml, requirements.lock  top-level pin; full hashed closure (linux amd64 + arm64)
  Dockerfile                         digest-pinned python:3.12-slim; stages base -> test -> runtime
../mock_apis/                        FastAPI carrier + AR services, own lock and Dockerfile, tests over real HTTP
../lock-deps.sh                      ./lock-deps.sh gateway | mock_apis
../docker-compose.yml                gateway on 127.0.0.1:${GATEWAY_PORT:-8765}; carrier-api, ar-api internal only
../tests/test_gateway_http.py        stdlib wire-level MCP tests against the running stack
```

## Run it (Docker Desktop)
```bash
make up P=01             # first run creates .env from .env.example with random mock-API secrets; waits until healthy
curl -s localhost:8765/healthz
make gateway-test P=01   # builds each image's `test` stage and runs it (no network); does not redeploy
make test P=01           # includes the raw JSON-RPC-over-HTTP contract tests while the stack is up
REQUIRE_GATEWAY=1 make test P=01   # CI: fail instead of skipping when the gateway is down
make live-check P=01     # every tool through MCP; result per tool with its configured backend
make down P=01
```
If you change `GATEWAY_PORT` in `.env`, export it too: Compose reads `.env`, but `make test` does not. With live CRM/ERP backends, run the wire tests as `GATEWAY_LIVE_BACKENDS=1 make test P=01`.

## MCP Inspector
```bash
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http --method tools/list
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http \
  --method tools/call --tool-name search_accounts --tool-arg query=BBO
npx -y @modelcontextprotocol/inspector@2.14.0 --web --transport http --server-url http://localhost:8765/mcp
```
Try these on the seed data:
- `search_accounts`: `Summit` or `BBO`
- `get_sales_order` and `track_shipment`: `SO-104233` or `SO-104301`
- `get_open_invoices`: `ACCT-1002`

## Updating dependencies
1. Edit the top-level pins in `gateway/pyproject.toml` or `mock_apis/pyproject.toml`.
2. From the project root, run `./lock-deps.sh gateway` (or `mock_apis`). It resolves inside the Dockerfile's digest-pinned base image and hashes the wheels for amd64 and arm64.
3. Run `make gateway-test P=01` and `make up P=01`.

To move the base image, update the digest in both Dockerfiles (`docker pull python:3.12-slim`, then `docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim`).

## Notes for later milestones
- Containers that call the gateway as `http://gateway:8000` (n8n, the copilot; Milestones 5-6) need `gateway:*` added to `GATEWAY_ALLOWED_HOSTS` in `../docker-compose.yml`. Otherwise they get 421.
- The Salesforce Run As user is the identity behind every CRM call. The setup guide recommends a dedicated minimum-access integration user now. The gateway's connector is GET-only either way.

## Why the copilot doesn't use it yet
The copilot (`../cs_copilot/`) still calls the in-process mocks. In Milestone 6 it switches to an MCP client (`McpToolSource` via `agentcore.register_tool_source`). From then on, the client's policy decides scopes and side effects; the server's own tool descriptions are never trusted for that.
