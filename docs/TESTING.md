# Testing & Verification

How to verify the broker: unit tests, then the local console, then a live MCP call
against whichever provider you configured.

Related: [Architecture](ARCHITECTURE.md) · [Deployment](DEPLOYMENT.md) · [Security](SECURITY.md) · [Agent Registry](AGENT_REGISTRY.md)

> [!IMPORTANT]
> The interactive browser console described below is **local-development only** and is
> not mounted unless `ENABLE_TEST_CONSOLE=true`. It serves unauthenticated endpoints
> that hand out a live user's proxy access token. Never enable it on a deployed
> service. See [Security](SECURITY.md#test-console).

> Every URL below is a placeholder. Substitute your own service URL.

---

## 1. Unit tests

68 tests cover the OAuth grants, redirect-URI allowlist, token expiry and revocation,
the storage backends, and the MCP proxy path. They run entirely offline.

```bash
pip install -r requirements.txt
TESTING=true python -m pytest tests/ -q
```

`TESTING=true` selects the in-memory storage backend and disables tracing, so no GCP
credentials or upstream configuration are required.

---

## 2. Run the broker locally

```bash
export ENABLE_TEST_CONSOLE=true
export STORAGE_BACKEND=memory
export UPSTREAM_SERVICE_NAME="<vendor>"
export UPSTREAM_CLIENT_ID="<client id from DCR or the vendor portal>"
export UPSTREAM_AUTH_URL="https://<auth-host>/authorize"
export UPSTREAM_TOKEN_URL="https://<auth-host>/token"
export UPSTREAM_MCP_URL="https://<mcp-host>/mcp"
export GE_CLIENT_SECRET="$(openssl rand -hex 24)"
export GE_ALLOWED_REDIRECT_URIS="http://localhost:8080/test-callback"

uvicorn app.main:app --reload --port 8080
# then open http://localhost:8080/test
```

Per-vendor endpoint values are listed in
[Deployment step 5](DEPLOYMENT.md#5-deploy-to-cloud-run) and in `deploy.sh`.

> [!NOTE]
> With `STORAGE_BACKEND=memory` the startup check for required settings is skipped, so
> the server boots even with the upstream endpoints unset — it then fails at sign-in
> instead of at startup. Set them.

---

## 3. Console walkthrough

The console exists only at `http://localhost:8080`. It is not reachable on a deployed
service, and enabling it there is a critical misconfiguration.

| Page | Purpose |
|---|---|
| `http://localhost:8080/test` | Landing page. Starts the 3-legged OAuth flow against the configured provider. |
| `http://localhost:8080/test-callback` | Receives the proxy authorization code and hosts the MCP test controls. |

The callback page has three steps:

1. **Proxy Authorization Code** — shows the code returned to `/test-callback`, or loads
   an existing session from `/test/active-token`.
2. **Gemini Enterprise Proxy Token** — calls `/test/exchange`, which performs the
   `authorization_code` exchange server-side so `GE_CLIENT_SECRET` never reaches the
   browser.
3. **Query the MCP server via the proxy** — three controls:
   - **Discover Tools (`tools/list`)** — returns the provider's advertised tool list.
     It also runs automatically once a token is available, and is the quickest check
     that the token swap, header sanitisation and SSE decoding all work.
   - **Initialize Session (`initialize`)** — sends an MCP `initialize` request
     (`protocolVersion: 2025-06-18`) and reports the server's capabilities.
   - **Call Tool** — a free-form runner: a tool name from `tools/list` in one field,
     its arguments as JSON in the other; the console then issues `tools/call`. There
     are no hardcoded tool buttons, because tool names and argument shapes differ per
     provider.

---

## 4. Direct cURL verification

```bash
SERVICE_URL="https://ge-<vendor>-proxy-<hash>.<region>.run.app"   # or http://localhost:8080
PROXY_TOKEN="<proxy access token>"
```

List what the provider actually exposes:

```bash
curl -s -X POST "${SERVICE_URL}/mcp" \
  -H "Authorization: Bearer ${PROXY_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Then call one of the tools it returned, using the argument names from that tool's
`inputSchema`:

```bash
curl -s -X POST "${SERVICE_URL}/mcp" \
  -H "Authorization: Bearer ${PROXY_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "<tool name from tools/list>",
      "arguments": {}
    }
  }'
```

Negative checks worth running against any deployment:

```bash
# Unknown bearer token must be rejected.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "${SERVICE_URL}/mcp" \
  -H "Authorization: Bearer not-a-real-token" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'        # expect 401

# Console must be absent on a deployed service.
curl -s -o /dev/null -w '%{http_code}\n' "${SERVICE_URL}/test"               # expect 404
curl -s -o /dev/null -w '%{http_code}\n' "${SERVICE_URL}/test/active-token"  # expect 404
```

---

## 5. Verifying through Gemini Enterprise

Once the connector is configured (see
[Deployment](DEPLOYMENT.md#connecting-gemini-enterprise)), sign in through the chat
card and ask prompts that map onto tools the provider advertises:

| Category | Sample prompt |
| :--- | :--- |
| **Context discovery** | *"What tools do you have access to from my connected account?"* |
| **Recent data** | *"Summarize my most recent records in the connected service."* |
| **Specific search** | *"Search my account records for updates related to project roadmap."* |

What to confirm:

- The sign-in card appears and the browser round-trip completes.
- Results are scoped to the signed-in user, not to whoever authenticated last. Sign in
  as a second user and confirm the answers differ.
- A query an hour later still works, exercising the transparent refresh path.

---

## Appendix: worked example against Metaview

> [!NOTE]
> This appendix records one provider's specifics. Tool names, arguments and product
> concepts differ per provider — do not treat anything here as MCP-general. Run
> `tools/list` against your own provider and use what it returns.

### A1. Interview data

1. Sign in to your Metaview account and make sure at least one recorded conversation
   exists.
2. In Gemini Enterprise, ask for a summary of recent interviews.
3. Direct call through the proxy:

   ```bash
   curl -s -X POST "${SERVICE_URL}/mcp" \
     -H "Authorization: Bearer ${PROXY_TOKEN}" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{
       "jsonrpc": "2.0",
       "id": "test-search",
       "method": "tools/call",
       "params": {
         "name": "search_conversations",
         "arguments": {"query": "Alex Rivera"}
       }
     }'
   ```

   > [!IMPORTANT]
   > Read this tool's argument schema from a live `tools/list` rather than copying
   > the example above; providers change schemas without notice. See
   > [Architecture section 4](ARCHITECTURE.md#4-vendor-case-study-metaview).

4. What happens underneath: the proxy resolves the presented bearer token to that
   user's stored upstream token, strips browser headers, sets
   `Accept: application/json, text/event-stream`, forwards to
   `https://mcp.metaview.ai/mcp`, and decodes the SSE response back to Gemini
   Enterprise.

### A2. AI sourcing pipeline (no media uploads needed)

1. Sign in to [my.metaview.app](https://my.metaview.app), open **Sourcing**, and create
   a **New Search** with a role description, for example
   *`Senior Python Engineer in London with Kubernetes and distributed systems experience`*.
   Metaview drafts an Ideal Candidate Profile (ICP) and populates matched candidates.
2. Sample Gemini Enterprise prompts:

   | Category | Sample prompt |
   | :--- | :--- |
   | **Pipeline discovery** | *"What active candidate sourcing searches do I have?"* |
   | **Top candidates** | *"Show me the top 3 candidates surfaced in my Senior Python Engineer search."* |
   | **ICP criteria** | *"What are the mandatory requirements in the ICP for the London Python role?"* |
   | **Pipeline health** | *"Break down candidate stages and outreach status in my sourcing pipeline."* |

3. Direct call through the proxy:

   ```bash
   curl -s -X POST "${SERVICE_URL}/mcp" \
     -H "Authorization: Bearer ${PROXY_TOKEN}" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{
       "jsonrpc": "2.0",
       "id": "test-list-sourcing",
       "method": "tools/call",
       "params": {
         "name": "list_sourcing_searches",
         "arguments": {}
       }
     }'
   ```

4. Because every call carries the per-user proxy token, Metaview's own authorizer
   decides what that user may see. The broker does not widen access.
