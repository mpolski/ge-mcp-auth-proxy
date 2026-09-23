# Gemini Enterprise Universal MCP Identity Broker Proxy

[![Tests](https://img.shields.io/badge/tests-68%20passed-brightgreen.svg)](#testing)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Google Cloud Run](https://img.shields.io/badge/Google%20Cloud-Run-4285F4.svg?logo=googlecloud&logoColor=white)](https://cloud.google.com/run)
[![Secret Manager](https://img.shields.io/badge/Google%20Cloud-Secret%20Manager-34A853.svg?logo=googlecloud&logoColor=white)](https://cloud.google.com/secret-manager)

An enterprise OAuth 2.0 identity broker and Model Context Protocol (MCP) runtime proxy connecting **Gemini Enterprise (Vertex AI Search / Discovery Engine)** to any OAuth-secured MCP SaaS provider (**Metaview.ai**, **Carta**, **Greenhouse**, etc.), with strict per-user data isolation.

Runs entirely on Google Cloud Run and Google Cloud Secret Manager. No third-party infrastructure or external databases required.

---

## Documentation

| Document | Contents |
|---|---|
| **[Architecture](docs/ARCHITECTURE.md)** | Why the proxy is needed, request flows, multi-vendor isolation model |
| **[Deployment](docs/DEPLOYMENT.md)** | Step-by-step gcloud runbook, multi-vendor presets, Gemini Enterprise setup |
| **[Security](docs/SECURITY.md)** | Threat model, per-user token isolation, Secret Manager TTLs, resolved issues |
| **[Testing](docs/TESTING.md)** | Unit test suite and live MCP tool verification |
| **[Agent Registry](docs/AGENT_REGISTRY.md)** | Optional: publishing the broker to the Gemini Enterprise tool catalog |

---

## How Gemini Enterprise reaches the broker

```
Employee ──▶ Gemini Enterprise ─────▶ Cloud Run broker ──▶ SaaS MCP server
                                            │
                                            ▼
                                     Secret Manager
                               (one token secret per user)
```

Gemini Enterprise is configured with the broker's HTTPS URL as a custom MCP
connector, and the broker acts as the OAuth 2.0 authorization server it
authenticates against. Nothing sits in between.

Registering in [Agent Registry](docs/AGENT_REGISTRY.md) is **optional** and only
affects catalog discoverability. It does not change the traffic path.

### Verification status

Being explicit about what has actually been exercised against a live system:

| Claim | Status |
|---|---|
| End-to-end OAuth sign-in through Gemini Enterprise | **Verified** against a live connector, 2026-09-23 |
| Per-user token isolation with concurrent users | **Verified** with two distinct users |
| Live `tools/call` returning upstream data | **Verified** against Metaview |
| Metaview endpoint configuration | **Verified** by end-to-end use |
| Carta and Greenhouse endpoint configuration | **Verified** from provider OAuth discovery documents; **not yet run end to end** |
| Routing via Agent Gateway | **Out of scope** for this release |

> [!NOTE]
> The Carta and Greenhouse profiles ship with endpoints read from each provider's
> own RFC 8414 / RFC 9728 metadata, so the URLs are correct. What has not happened
> is a full sign-in against a real tenant of either, which needs an account. Expect
> to discover tenant-specific details on first run.

> [!IMPORTANT]
> There is no running reference deployment. The environment used for the
> verification above has been torn down, so nothing here is live and no
> credentials, project IDs or service URLs from it remain in this repository.
> Deploy into your own project with your own client registrations.

---

## Enterprise Governance Meets Desktop-Centric Protocols

### Architectural Context

**Gemini Enterprise (Vertex AI Search / Discovery Engine)** enforces enterprise-grade security, identity governance, and compliance:
- Utilizes pre-registered, audited OAuth 2.0 client credentials (RFC 6749) and PKCE verification (RFC 7636).
- Operates a standardized, centrally managed enterprise redirect URI (`https://vertexaisearch.cloud.google.com/oauth-redirect`) to prevent unauthorized domain redirection.
- Enforces strict per-user identity boundaries so conversational AI queries never leak confidential records across organizational roles.

In contrast, emerging Model Context Protocol (MCP) servers (such as Metaview, Carta, and Greenhouse tools) were originally engineered for individual desktop developer runtimes (like Cursor, Claude Desktop, or local CLI agents). These desktop-centric tools rely on dynamic, on-the-fly client registration (RFC 7591) and ephemeral localhost callbacks without an IT administrative portal for corporate tenant pre-registration.

| Architectural Requirement | Gemini Enterprise (Enterprise Cloud Standards) | Desktop-Centric MCP Servers (Workstation Model) |
|---|---|---|
| **Client Registration** | Pre-registered, audited client credentials (RFC 6749) | Dynamic Client Registration (RFC 7591) or desktop PKCE |
| **Redirect URI** | Managed enterprise redirect URI: `vertexaisearch.../oauth-redirect` | Localhost or dynamic schemes; no tenant whitelisting console |
| **Content Negotiation** | Standard enterprise JSON-RPC client | Often mandates `Accept: application/json, text/event-stream` (406 otherwise) |
| **Gateway Security** | Standard enterprise browser/cloud headers | Upstream API gateways drop unwhitelisted `Origin` headers (403) |
| **Data Boundary** | Strict per-user identity boundaries | Per-user OAuth tokens required; shared API keys break compliance |

### The Enterprise Solution

Rather than resorting to insecure compromises—such as sharing a static API key across employees, which would violate enterprise access control and confidentiality policies—this **Google Cloud Run Identity Broker** provides a secure, enterprise-grade integration tier:

1. **Complies with Gemini Enterprise's Governance:** Presents a standard RFC 6749 authorization server with static credentials, PKCE verification, and managed redirect handling.
2. **Bridges Desktop-Centric MCP Providers:** Dynamically executes RFC 7591 registration, PKCE negotiation, and protocol/header normalization with the upstream SaaS provider.
3. **Guarantees Per-User Data Isolation:** Encrypts and manages individual user tokens in Google Cloud Secret Manager with native automated TTL expiration.

```
Employee ──▶ Gemini Enterprise ──▶ Cloud Run Identity Broker ──▶ Upstream MCP Server
                                           │                   (Metaview, Carta, Greenhouse)
                                           ▼
                                   Secret Manager
                             (one token secret per user)
```

---

## Separation of Concerns: Dedicated Instances per Vendor

To ensure failure domain isolation, security partitioning, and seamless alignment with Gemini Enterprise:

- **Dedicated Cloud Run Service per Vendor:** Each SaaS provider runs on its own isolated Cloud Run service (`ge-metaview-proxy`, `ge-carta-proxy`, `ge-greenhouse-proxy`).
- **Failure Domain Isolation:** A schema change, token revocation spike, or vendor outage on one SaaS platform has zero blast radius on others.
- **Dedicated Secret Manager Namespaces:** Ephemeral user tokens and sessions use separate prefixes derived from the vendor name (`ge-meta-*`, `ge-cart-*`, `ge-gree-*`), preventing cross-service credential leakage.
- **Dedicated Gemini Enterprise MCP Registrations:** Gemini Enterprise registers tools per endpoint URL; dedicated services provide 1:1 mapped endpoint URLs.

---

## Quickstart

### 1. Deploy to Cloud Run

The included `deploy.sh` script automates enabling GCP APIs, creating least-privilege IAM roles, creating Secret Manager credentials, deploying Cloud Run, and performing Dynamic Client Registration.

`VENDOR` is required and has no default, so a deployment cannot silently point at
the wrong provider.

#### Deploy for Metaview
```bash
export GCP_PROJECT_ID="your-project-id"
VENDOR=metaview ./deploy.sh
```

#### Deploy for Carta

Carta's MCP server requires a **public PKCE client** — its token endpoint offers no
`client_secret` auth method at all. Leave `UPSTREAM_CLIENT_SECRET` unset and let
Dynamic Client Registration supply the client ID.

```bash
export GCP_PROJECT_ID="your-project-id"
VENDOR=carta ./deploy.sh
```

#### Deploy for Greenhouse
```bash
export GCP_PROJECT_ID="your-project-id"
VENDOR=greenhouse ./deploy.sh
```

#### Deploy for Any Custom MCP Service

Any `VENDOR` name outside the three presets works, provided you supply the
endpoints. Discover them from the provider itself rather than guessing:

```bash
curl -s -D- -X POST https://<mcp-host>/mcp        # read the WWW-Authenticate header
curl -s https://<mcp-host>/.well-known/oauth-protected-resource/mcp
curl -s https://<auth-host>/.well-known/oauth-authorization-server
```

```bash
export GCP_PROJECT_ID="your-project-id"
export UPSTREAM_AUTH_URL="https://auth.example.com/oauth2/authorize"
export UPSTREAM_TOKEN_URL="https://auth.example.com/oauth2/token"
export UPSTREAM_MCP_URL="https://mcp.example.com/mcp"
export UPSTREAM_CLIENT_ID="your-client-id"   # or omit and let DCR supply it
VENDOR=mycustom ./deploy.sh
```

Prefer running raw `gcloud` commands manually? See [Deployment → Manual path](docs/DEPLOYMENT.md#manual-path).

### 2. Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # edit credentials

export STORAGE_BACKEND=memory
export ENABLE_TEST_CONSOLE=true
uvicorn app.main:app --reload --port 8080
```

### 3. Run Tests

```bash
TESTING=true python -m pytest tests/ -q
```

---

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /oauth/authorize` | Initiates 3-legged user sign-in; redirects to upstream provider |
| `GET /oauth/callback` | Receives upstream auth code, mints proxy authorization code |
| `POST /oauth/token` | Exchanges auth code or refresh token for a proxy bearer token |
| `POST /oauth/revoke` | RFC 7009 token revocation; terminates user sessions and deletes secrets |
| `POST /mcp`, `GET /mcp` | MCP JSON-RPC proxy with per-user token swap and SSE translation |
| `GET /health` | Liveness and readiness health probe |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 OAuth 2.0 authorization server metadata |

---

## Key Configuration Variables

Set via environment variables or `.env` file (see [app/config.py](app/config.py) and [.env.example](.env.example)):

| Variable | Default | Description |
|---|---|---|
| `PROXY_BASE_URL` | `http://localhost:8080` | Public URL of this Cloud Run service |
| `GE_CLIENT_ID` | `ge-mcp-proxy-client` | Client ID expected from Gemini Enterprise |
| `GE_CLIENT_SECRET` | — (required) | Mounted securely from Secret Manager via `--set-secrets` |
| `UPSTREAM_SERVICE_NAME`| `upstream` | Identifier for the upstream vendor (e.g. `metaview`, `carta`, `greenhouse`) |
| `UPSTREAM_CLIENT_ID` | — (required) | Upstream OAuth client ID (from DCR or developer portal) |
| `UPSTREAM_CLIENT_SECRET` | — | Upstream OAuth client secret. Leave empty for public PKCE clients; Carta rejects a request that carries one |
| `UPSTREAM_AUTH_URL` | — (required) | Upstream OAuth authorization endpoint |
| `UPSTREAM_TOKEN_URL` | — (required) | Upstream OAuth token endpoint |
| `UPSTREAM_MCP_URL` | — (required) | Upstream hosted MCP server URL |
| `UPSTREAM_RESOURCE` | `None` | RFC 8707 Resource indicator, normally the MCP URL, if the provider requires it |
| `UPSTREAM_AUDIENCE` | `None` | Audience parameter (e.g. Auth0/Okta backed services) |
| `STORAGE_BACKEND` | `secret_manager` | Storage engine (`secret_manager` for GCP, `memory` for local testing) |
| `SECRET_PREFIX` | `ge-<first 4 chars of vendor>` | Secret Manager name prefix. Must be unique per provider in a shared project |
| `PROXY_ACCESS_TOKEN_EXPIRES_IN` | `3600` | Proxy access token validity enforced at `/mcp` (1 hour) |
| `PROXY_REFRESH_TOKEN_EXPIRES_IN` | `2592000` | Absolute refresh session ceiling (30 days) |
| `OTEL_ENABLED` | `true` | Export OpenTelemetry traces to Google Cloud Trace |
| `GE_ALLOWED_REDIRECT_URIS` | GE callback | Comma-separated exact-match allowlist of `redirect_uri` values accepted at `/oauth/authorize` (RFC 6749 §3.1.2.3). `*` disables enforcement (local development only) |

> [!IMPORTANT]
> The variables marked *required* have no default. If any of them is unset the
> service refuses to start rather than booting with a half-configured upstream,
> which would otherwise only surface as a failure part-way through a user's
> sign-in. The check is skipped when `STORAGE_BACKEND=memory` so local runs and
> the test suite still work.

---

## Repository Layout

```
app/
  main.py            FastAPI application entrypoint and route mounting
  config.py          Pydantic settings and vendor configuration
  telemetry.py       OpenTelemetry / Google Cloud Trace integration
  oauth/
    client.py        Upstream OAuth 2.0 client (PKCE, DCR, token exchange, refresh)
    router.py        RFC 6749 authorization server endpoints (/oauth/authorize, /oauth/token, /oauth/revoke)
    pkce.py          RFC 7636 PKCE code verifier and challenge utilities
    dcr.py           RFC 7591 Dynamic Client Registration CLI & helper
  mcp/
    proxy.py         MCP JSON-RPC proxy, SSE streaming, and per-user token swap
  storage/
    base.py          Abstract storage backend and Pydantic models (UserTokenData, OAuthSessionData)
    secret_manager.py Google Cloud Secret Manager backend with native TTL management
    memory.py        Ephemeral in-memory backend for unit tests and local dev
  test_ui.py         Local-only browser test console (disabled in production)
docs/
  ARCHITECTURE.md    Architectural deep dive and request flows
  DEPLOYMENT.md      Step-by-step gcloud runbook and Gemini Enterprise setup
  SECURITY.md        Threat model, isolation guarantees, and Secret Manager TTLs
  TESTING.md         Unit tests and MCP verification guide
  AGENT_REGISTRY.md  Optional catalog registration runbook
deploy.sh            Multi-vendor deployment automation script
toolspec.json        Example read-only tool allowlist (Metaview profile) for Agent Registry
tests/               68 automated pytest test cases
```
