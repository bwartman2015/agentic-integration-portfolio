# Gateway (MCP server)

The Ridgeline enterprise MCP gateway is the one controlled **environment** that every agent in the portfolio acts through. It is built on the official MCP Python SDK (`mcp==2.3.0`, `MCPServer`, which was called FastMCP before SDK 2.x) and serves **streamable HTTP** at `/mcp`. All data is synthetic (fictional Ridgeline Supply Co.).

## Status: Milestone 3 (authentication). Every /mcp call needs a Keycloak access token; connectors (M2) live
| | Now | Arrives in |
|---|---|---|
| Tools | Four read-only tools: `search_accounts`, `get_sales_order`, `track_shipment`, `get_open_invoices`. They keep the copilot's names, arguments and output shapes. | M5: `create_case` (write) behind the n8n approval |
| Connectors | One per system, each with a `seed` backend (offline synthetic data) and a live backend, chosen in `.env`. The table below lists them. | |
| Choke point | Every `tools/call` goes through `GatewayServer.call_tool`. It rejects unknown tools and unexpected arguments and logs every outcome. | M3 JWT, M4 OPA and M5 audit plug in here |
| Input validation | Closed JSON Schemas (`additionalProperties: false`):<br>• `sales_order` must match `^SO-[0-9]{6}$`<br>• `account_id` must match `^ACCT-[0-9]{4}$`<br>• `query` must match `^[A-Za-z0-9][A-Za-z0-9 &.,'-]{1,79}$`<br>The gateway accepts only canonical IDs; clients normalise case and whitespace. | |
| Secrets | Read only from the environment and masked in every repr. Each container gets only its own variables. A half-configured live backend stops startup (fail closed). | |
| Writes | None from the gateway. Its connectors may only send `GET` (plus the Salesforce token `POST`), enforced in `HttpConnector`. The operator seed tool alone builds a `POST`/`PATCH` Salesforce connector, and it never sends `DELETE` or `PUT`. | M5: `create_case` behind approval |
| Transport security | Published on `127.0.0.1` only. Host and Origin allowlist (a bad Host gets 421, a bad Origin 403). The containers are non-root and read-only, with all capabilities dropped and `restart: "no"`. The mock APIs sit on an internal-only Docker network (`internal: true`: no route to the host or the internet). Images are local builds only (`pull_policy`). | |
| Identity | **Required.** A Keycloak access token on every `/mcp` call (`auth.py`). The gateway checks:<br>• RS256 signature against Keycloak's keys<br>• exact `iss` and `aud` = `ridgeline-gateway`<br>• `typ` Bearer and an `azp` client id<br>• `exp` exactly; 5 s clock leeway on `nbf`/`iat`<br>Anything else gets 401 with a `WWW-Authenticate` challenge pointing to `/.well-known/oauth-protected-resource/mcp`. It fails closed: an unverifiable token gets 401, cached keys are dropped after Keycloak has been unreachable for 1 hour, and the gateway won't start without `OIDC_*`. | Consider `stateless_http=True` with per-request JWTs |
| Authorization | Not yet. Any valid client can call every tool, including the scope-less test client `p01-expiry-test`; the token's `scopes` are logged | M4: OPA per-tool scopes and region data rule; must deny `p01-expiry-test` every tool |
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
  ridgeline_gateway/auth.py          Keycloak JWT verifier (TokenVerifier), AuthConfig, principal for logs
  ridgeline_gateway/connectors/      base (HttpConnector, errors, upstream info), seed, salesforce, sap, http_mocks
  ridgeline_gateway/admin/           live_check (all tools through MCP, as cs-rep), token (make token),
                                     seed_salesforce (synthetic accounts)
  tests/                             config, connectors (httpx2.MockTransport), tool contracts + logging,
                                     admin tools (seed_salesforce against a fake org, live_check)
  pyproject.toml, requirements.lock  top-level pin; full hashed closure (linux amd64 + arm64)
  Dockerfile                         digest-pinned python:3.12-slim; stages base -> test -> runtime
../mock_apis/                        FastAPI carrier + AR services, own lock and Dockerfile, tests over real HTTP
../lock-deps.sh                      ./lock-deps.sh gateway | mock_apis
../docker-compose.yml                gateway on 127.0.0.1:${GATEWAY_PORT:-8765}; keycloak on 127.0.0.1:${KEYCLOAK_PORT:-8080};
                                     carrier-api, ar-api internal only; ops (profile) for live-check/token
../keycloak/ridgeline-realm.json     realm, client scopes, clients (secrets are ${ENV} placeholders)
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

## Identity (Keycloak)
- Keycloak 26.8.2 is pinned by digest and published on `127.0.0.1:${KEYCLOAK_PORT:-8080}`. It runs in dev mode: no TLS, H2 database, loopback only.
- Realm `ridgeline` is imported from `../keycloak/ridgeline-realm.json` on every start, with secrets substituted from `.env`.
- Admin console: `http://localhost:8080/` (or your `KEYCLOAK_PORT`), user `admin`, password = `KC_ADMIN_PASSWORD` in `.env`.
- Hardening:
  - Agent clients have only the service-account (client credentials) grant: standard, implicit, password, device, CIBA and token-exchange flows are off.
  - `fullScopeAllowed` is false, there are no realm default scopes and no users, and tokens last 5 minutes with no refresh tokens.
  - `bruteForceProtected` only guards user logins. Client secrets are 192-bit random values.
  - `sslRequired: external` is fine on loopback; set it to `all` before exposing Keycloak anywhere else.

| Client | Scopes in its token | Use |
|---|---|---|
| `cs-rep` | `crm.read erp.read shipping.read ar.read` | the copilot acting for a rep; `make live-check` |
| `cs-lead` | the four reads plus `crm.write` | the copilot acting for a lead (case writes need approval, M5) |
| `p01-expiry-test` | none (audience only), 5-second tokens | the wire test that proves an expired token gets 401 |

Every token's issuer is `http://localhost:${KEYCLOAK_PORT:-8080}/realms/ridgeline` (`KC_HOSTNAME`), whether requested from your Mac or from a container. The gateway fetches signing keys internally from `http://keycloak:8080/.../certs`.

Keycloak's database is in memory, so every Keycloak restart re-imports the realm with **new signing keys**. After restarting only Keycloak, wait up to 60 s (the gateway's unknown-key refetch limit) or restart the gateway too; `make up` handles both.

`make token P=01 CLIENT=cs-rep` prints a 5-minute token. It runs in the `ops` container, which holds the client secrets; the gateway container never does.

## MCP Inspector
Every call needs a token now. In the web UI:
1. Run `make token P=01 CLIENT=cs-rep` and copy the output.
2. Open **Authentication**, set **Header Name** to `Authorization` and **Bearer Token** to the token. Or add a custom header: `Authorization: Bearer <token>`.
3. Click **Connect**.

The token expires after 5 minutes; get a new one and reconnect. In the CLI, pass the header with `--header "Authorization: Bearer $TOKEN"`.
Without a token, the Inspector follows the 401 challenge to Keycloak and offers an interactive OAuth sign-in. That can't work here by design: the realm has no users, only agent machine identities (Keycloak's built-in account and admin consoles exist but have nobody to log in). The CLI reports it as `auth_required`. Paste a token instead.
```bash
TOKEN=$(make -s token P=01 CLIENT=cs-rep)          # from the repo root; stays in this shell only
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http \
  --header "Authorization: Bearer $TOKEN" --method tools/list
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://localhost:8765/mcp --transport http \
  --header "Authorization: Bearer $TOKEN" --method tools/call --tool-name search_accounts --tool-arg query=BBO
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
- `gateway:*` is in `GATEWAY_ALLOWED_HOSTS`, so containers on the project network (`ops` now; n8n and the copilot in M5-6) can call `http://gateway:8000/mcp`. They still need a token.
- The Salesforce Run As user is the identity behind every CRM call. The setup guide recommends a dedicated minimum-access integration user now. The gateway's connector is GET-only either way.

## Why the copilot doesn't use it yet
The copilot (`../cs_copilot/`) still calls the in-process mocks. In Milestone 6 it switches to an MCP client (`McpToolSource` via `agentcore.register_tool_source`). From then on, the client's policy decides scopes and side effects; the server's own tool descriptions are never trusted for that.
