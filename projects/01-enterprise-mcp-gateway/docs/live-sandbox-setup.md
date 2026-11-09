# Live sandbox setup: Salesforce and SAP

The gateway works offline by default. Milestone 2's live check needs two free accounts:

| System | Gateway tool | Backend setting | What you need |
|---|---|---|---|
| Salesforce Developer Edition | `search_accounts` | `CRM_BACKEND=salesforce` | An External Client App using the OAuth 2.0 **client credentials** flow |
| SAP Business Accelerator Hub sandbox | `get_sales_order` | `ERP_BACKEND=sap_sandbox` | Your personal API key |

The mock carrier and AR APIs need nothing from you. `make up P=01` generates their shared secrets.

> **Secrets go only in `projects/01-enterprise-mcp-gateway/.env`.** That file is git-ignored and created with owner-only permissions. Never paste a secret into a chat, an issue, a commit or a screenshot. If one leaks, rotate it: in Salesforce, regenerate the consumer secret; in SAP, sign out and back in to get a new key.

---

## 1. Salesforce Developer Edition

### 1a. Create the org (about 5 minutes)
1. Go to `https://developer.salesforce.com/signup` and sign up. Use a username shaped like an email that nobody else has used (for example `you.ridgeline@example.com`; it doesn't have to be a real inbox). The **Email** field must be real, because the activation link goes there.
2. Activate the account from the email and set a password.
3. In the org, go to **Setup** (gear icon) → search **My Domain**. Copy the **Current My Domain URL**. It looks like `https://yourname-dev-ed.develop.my.salesforce.com`. This is your `SF_LOGIN_URL`.
   - The client credentials flow **only works on the My Domain URL**, not on `login.salesforce.com`.

### 1b. Create the External Client App (about 10 minutes)
1. **Setup** → search **External Client App Manager** → **New External Client App**.
2. **Basic Information**:
   - Name: `Ridgeline MCP Gateway`
   - Contact email: your email
   - Distribution state: **Local**
3. **API (Enable OAuth Settings)** → tick **Enable OAuth**:
   - Callback URL: `https://login.salesforce.com/services/oauth2/success`. The client credentials flow never uses it, but the form requires one.
   - OAuth scopes: add **Manage user data via APIs (api)**. Nothing else.
   - Flow Enablement: tick **Enable Client Credentials Flow** and acknowledge the warning.
   - Security: keep **Require secret for Web Server Flow** and **Require secret for Refresh Token Flow** ticked. Leave PKCE as it is.
4. **Create**.
5. Open the app → **Policies** tab → **Edit** → **OAuth Policies**:
   - Tick **Enable Client Credentials Flow**.
   - **Run As (Username)**: the user every API call runs as, with that user's permissions.
     - **Quick start:** pick your own System Administrator user. The gateway's CRM connector can only send `GET` requests, enforced in code. Only the operator seed tool can write, and only `POST`/`PATCH` on its own synthetic records.
     - **Recommended:** a dedicated integration user with minimum access. In **Setup** → **Users** → **New User**, use the **Salesforce Integration** license (Developer Edition includes it). Assign it the **Salesforce API Integration** permission set license; object permissions for this license can only come from permission sets that use it. Then give it a permission set with *API Enabled* and *Read/Create/Edit* on Account and Contact. Create/Edit is needed only for `make seed-salesforce`; remove it afterwards for read-only. Then pick that user as Run As.
   - **Save**.
6. **Settings** tab → **OAuth Settings** → **Consumer Key and Secret**. Salesforce emails you a verification code; enter it.
   - Copy the **Consumer Key**. This is `SF_CLIENT_ID`.
   - Copy the **Consumer Secret**. This is `SF_CLIENT_SECRET`.
7. Wait up to **10 minutes**. New External Client Apps take a while to become active, and until then the token request fails with `invalid_client`.

Optional check from your terminal: a token request that returns an `access_token` means the app is ready. Don't paste the output anywhere.
```bash
# after step 3, from projects/01-enterprise-mcp-gateway (loads .env into this shell only)
set -a; . ./.env; set +a
curl -s -X POST "$SF_LOGIN_URL/services/oauth2/token" -d grant_type=client_credentials \
  --data-urlencode "client_id=$SF_CLIENT_ID" --data-urlencode "client_secret=$SF_CLIENT_SECRET" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if 'access_token' in d else d)"
```

## 2. SAP Business Accelerator Hub API key (about 3 minutes)
1. Go to `https://api.sap.com` and **Log On**. Register for a free SAP account if you don't have one.
2. Search for **Sales Order (A2X)**. This is the OData v2 service `API_SALES_ORDER_SRV` under *SAP S/4HANA Cloud Public Edition*. Open it.
3. Click **Show API Key** (top right of the API page) → **Copy Key to Clipboard**. This is `SAP_API_KEY`.
   - The key belongs to your SAP account and works for every sandbox API on the hub.
   - The sandbox is read-only SAP demo data and is rate limited. The gateway only reads.

Leave `SAP_BASE_URL` at its default: `https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_SALES_ORDER_SRV`.

## 3. Put the values in `.env`
`make up P=01` already created `.env` from `.env.example`, with random mock-API secrets. Don't copy `.env.example` over it. Edit the existing file instead:
```bash
cd ~/agentic-integration-portfolio/projects/01-enterprise-mcp-gateway
open -e .env        # TextEdit; save as plain text
```
Change these lines (no quotes, no spaces around `=`):
```
CRM_BACKEND=salesforce
SF_LOGIN_URL=https://yourname-dev-ed.develop.my.salesforce.com
SF_CLIENT_ID=<Consumer Key>
SF_CLIENT_SECRET=<Consumer Secret>

ERP_BACKEND=sap_sandbox
SAP_API_KEY=<SAP API key>
```
If `.env` doesn't exist yet, run `make up P=01` once first. Don't create it with `cp`: the `__GENERATE__` placeholders would stay as literal text, and the mock APIs refuse to start.

## 4. Seed Salesforce, then run the live check
From the repo root:
```bash
make up P=01                                   # restarts the gateway with the new settings
make seed-salesforce P=01                      # dry run: lists the accounts/contacts it would create
make seed-salesforce P=01 ARGS=--apply         # writes 8 synthetic Ridgeline accounts + contacts
make live-check P=01                           # all 4 tools through MCP, with the backend for each
```
Expected `live-check` output once everything is live. The backend column shows what `.env` configures; the gateway's log line for each call records what actually answered (`docker compose -f projects/01-enterprise-mcp-gateway/docker-compose.yml logs gateway`):
```
tool               backend      result  summary   (backend = configured in .env)
search_accounts    salesforce   ok      1 match(es): Summit Contractors LLC (ACCT-1002)
get_sales_order    sap_sandbox  ok      SO-000001 status=... total=... USD
track_shipment     carrier_api  ok      1 shipment(s), first status=EXCEPTION
get_open_invoices  ar_api       ok      1 open invoice(s), total_open=8370.0
all tools returned data
```
Notes:
- **Alias search (`BBO`) uses SOSL.** Salesforce indexes new records for search after a few minutes. Name and account-number searches work right away.
- **The seed script is idempotent and never deletes.**
  - It matches accounts on `AccountNumber` (`ACCT-xxxx`) and refuses anything that isn't synthetic (emails must be `@example.com`).
  - It updates an existing account only if the seeder created it (its Description starts with `Ridgeline synthetic account (fictional)`). Any other account with the same number is skipped and reported.
  - Running it again creates nothing new.
- **SAP's order numbers map to `SO-` plus 6 digits** (`1` becomes `SO-000001`). `live-check` asks the sandbox for an existing number first. Ridgeline's demo scenario (SO-104233) exists only in the seed ERP, so the copilot demo keeps `ERP_BACKEND=seed`.
- **With live CRM/ERP, run the wire tests as `GATEWAY_LIVE_BACKENDS=1 make test P=01`.** That skips the two comparisons against seeded CRM/ERP data.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `make up` fails and `docker compose logs gateway` shows `startup_failed ... SF_CLIENT_ID is required` | A live backend is selected but a value is missing or misspelled in `.env`. The gateway fails closed by design. |
| `startup_failed ... SF_LOGIN_URL must be https://<your-domain>.my.salesforce.com` | Use the My Domain URL from step 1a, starting with `https://`. |
| `Salesforce returned an error (HTTP 400)` | `invalid_client` or `invalid_grant`: the app isn't active yet (wait 10 minutes), the client credentials flow or the Run As user isn't set (step 1b.5), or the key or secret is wrong. |
| `Salesforce rejected the gateway's credentials.` | HTTP 401: the token was refused even after a refresh. Check that the External Client App is still enabled and the secret hasn't been rotated. |
| `Salesforce refused the request (not permitted, or an API limit was reached).` | HTTP 403. Three possible causes:<br>• the Run As user lacks **API Enabled** or access to Account/Contact;<br>• the org's daily API limit is used up (`REQUEST_LIMIT_EXCEEDED`);<br>• the API is disabled for the org. |
| `SAP rejected the gateway's credentials.` | Wrong or expired API key. Copy it again from api.sap.com. |
| `SAP is rate limiting the gateway` | The sandbox throttles requests. Wait a minute. |
| Every SAP order comes back "not found" | A wrong `SAP_BASE_URL` path on `api.sap.com` also answers 404. Reset it to the default in `.env.example`. |
| Alias search returns nothing | SOSL indexing lag. Retry after a few minutes, or search by name. |
