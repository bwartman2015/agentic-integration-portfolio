# Gateway (MCP server)

The Ridgeline enterprise MCP gateway is the one controlled **environment** that every agent in the portfolio acts through. It is built on the official MCP Python SDK (`mcp==2.3.0`, `MCPServer`, which was called FastMCP before SDK 2.x) and serves **streamable HTTP** at `/mcp`. All data is synthetic (fictional Ridgeline Supply Co.).

## Status: Milestone 1 (server skeleton)
| | Now | Arrives in |
|---|---|---|
| Tools | `get_sales_order` (read-only), backed by the seeded mock ERP in `../mock_systems/` | M2: Salesforce, SAP sandbox, carrier and AR connectors |
| Choke point | Every `tools/call` goes through `GatewayServer.call_tool`. It rejects unknown tools and unexpected arguments and logs every outcome. | M3 JWT, M4 OPA and M5 audit plug in here |
| Input validation | Closed JSON Schema from type hints: `sales_order` must match `^SO-[0-9]{6}$`, with `additionalProperties: false`. The gateway accepts only canonical IDs; clients normalise case and whitespace (the copilot does this in `cs_copilot/agent/tools.py`). | |
| Transport security | Published on `127.0.0.1` only. Host and Origin allowlist (DNS-rebinding protection): a bad Host gets 421, a bad Origin 403. The container is non-root and read-only, with all capabilities dropped and `restart: "no"`. | |
| Identity | **None yet. Do not expose this port beyond localhost.** | M3: Keycloak JWT validation (401 without a valid token); consider `stateless_http=True` with per-request JWTs |
| Authorization | None yet | M4: OPA per-tool scopes and region data rule |
| Observability | One JSON line per call: `{"event": "tool_call", "tool", "outcome", "ok", "latency_ms", "request_id", "traceparent"?, "x_request_id"?, "error_type"?}`. Trace headers are logged only when well-formed (otherwise `"invalid"`). Argument values and unknown tool names are not logged, and SDK INFO logs are silenced. Crashes reach the client only as `Error executing tool <name>`. | M5: audit table, rate limits, PII masking, approvals |

## Layout
```
gateway/
  ridgeline_gateway/server.py   GatewayServer (MCPServer subclass), tools, /healthz, transport security
  tests/test_server.py          in-process contract tests (SDK client); run in the image's test stage
  pyproject.toml                top-level pins
  requirements.lock             full dependency closure, sha256-hashed (linux amd64 + arm64)
  lock-deps.sh                  regenerates requirements.lock inside the pinned base image
  Dockerfile                    digest-pinned python:3.12-slim; stages base -> test -> runtime
../docker-compose.yml           gateway service on 127.0.0.1:${GATEWAY_PORT:-8765}
../tests/test_gateway_http.py   stdlib wire-level MCP test against the running container
```

## Run it (Docker Desktop)
```bash
make up P=01             # build + start, waits until the healthcheck passes
curl -s localhost:8765/healthz
make gateway-test P=01   # builds the `test` stage and runs the gateway tests in it (does not redeploy)
make test P=01           # includes the raw JSON-RPC-over-HTTP contract tests while the gateway is up
REQUIRE_GATEWAY=1 make test P=01   # CI: fail instead of skipping when the gateway is down
make down P=01
```
If you change `GATEWAY_PORT` in `.env`, export it too: Compose reads `.env`, but `make test` does not.

## MCP Inspector
```bash
# CLI
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http --method tools/list
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http \
  --method tools/call --tool-name get_sales_order --tool-arg sales_order=SO-104233

# Web UI (opens http://127.0.0.1:6274 with a one-time session token)
npx -y @modelcontextprotocol/inspector@2.14.0 --web --transport http --server-url http://localhost:8765/mcp
```
- `SO-104233` returns Summit Contractors' order.
- `SO-999999` returns a tool error (`isError: true`, "was not found in ERP").
- `104233`, `so-104233` and any extra argument are rejected.

## Updating dependencies
1. Edit the top-level pin in `pyproject.toml`.
2. Run `gateway/lock-deps.sh` from the project root. It resolves inside the Dockerfile's digest-pinned base image and hashes the wheels for amd64 and arm64.
3. Run `make gateway-test P=01` and `make up P=01`.

To move the base image, update the digest in the Dockerfile first (`docker pull python:3.12-slim` then `docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim`).

## Notes for later milestones
- Containers that call the gateway as `http://gateway:8000` (n8n, the copilot; Milestones 5-6) need `gateway:*` added to `GATEWAY_ALLOWED_HOSTS` in `../docker-compose.yml`. Otherwise they get 421.
- Connector errors (M2) must be raised as `ToolError` with sanitised text. Anything else is reported to the client only as a generic failure.

## Why the copilot doesn't use it yet
The copilot (`../cs_copilot/`) still calls the in-process mocks. In Milestone 6 it switches to an MCP client (`McpToolSource` via `agentcore.register_tool_source`). From then on, the client's policy decides scopes and side effects; the server's own tool descriptions are never trusted for that.
