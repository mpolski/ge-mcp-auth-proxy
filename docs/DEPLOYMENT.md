# Deployment

Deploy the Gemini Enterprise Universal MCP Identity Broker Proxy to Google Cloud Run.

Related: [Architecture](ARCHITECTURE.md) · [Security](SECURITY.md) · [Testing](TESTING.md) · [Agent Registry](AGENT_REGISTRY.md)

---

## Architectural Model: One Cloud Run Instance Per Vendor

To maintain strict **separation of concerns, fault domain isolation, and dedicated Secret Manager namespaces**, each SaaS provider runs as its own separate Cloud Run service:

- `ge-metaview-proxy`
- `ge-greenhouse-proxy`
- `ge-carta-proxy`

Gemini Enterprise registers each tool against its corresponding Cloud Run URL. An issue or token refresh spike with one provider cannot impact any other service.

---

## Quick Path (`deploy.sh`)

The included `deploy.sh` script automates enabling GCP APIs, creating least-privilege IAM roles, creating Secret Manager credentials, deploying Cloud Run, and executing RFC 7591 Dynamic Client Registration where supported.

> [!IMPORTANT]
> `VENDOR` is **required**. There is no default vendor: the script exits if it is unset,
> and the application itself refuses to start without upstream endpoints. `metaview`,
> `carta` and `greenhouse` are preset profiles; any other name is accepted but you must
> supply `UPSTREAM_AUTH_URL`, `UPSTREAM_TOKEN_URL` and `UPSTREAM_MCP_URL` yourself.


### Deploy Metaview
```bash
export GCP_PROJECT_ID="<YOUR_PROJECT_ID>"
VENDOR=metaview ./deploy.sh
```

### Deploy Carta

Carta advertises `token_endpoint_auth_methods_supported = ["none", "private_key_jwt"]`,
so there is no client secret option at all: leave `UPSTREAM_CLIENT_SECRET` empty and
deploy as a public PKCE client. The client ID is obtained automatically via Dynamic
Client Registration.

```bash
export GCP_PROJECT_ID="<YOUR_PROJECT_ID>"
VENDOR=carta ./deploy.sh
```

### Deploy Greenhouse
```bash
export GCP_PROJECT_ID="<YOUR_PROJECT_ID>"
VENDOR=greenhouse ./deploy.sh
```

### Deploy another provider

```bash
export GCP_PROJECT_ID="<YOUR_PROJECT_ID>"
VENDOR=acme \
  UPSTREAM_AUTH_URL="https://auth.example.com/authorize" \
  UPSTREAM_TOKEN_URL="https://auth.example.com/token" \
  UPSTREAM_MCP_URL="https://mcp.example.com/mcp" \
  ./deploy.sh
```

`deploy.sh` is a thin wrapper around the numbered steps below. If you prefer, run each step manually.

---

## Manual Path

### 0. Set your variables

```bash
export PROJECT_ID="<YOUR_PROJECT_ID>"
export REGION="us-central1"
export VENDOR="metaview"  # required: metaview, carta, greenhouse, or your own name
export SERVICE_NAME="ge-${VENDOR}-proxy"
export SA_NAME="${VENDOR}-proxy-sa"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
# Secret Manager namespace: ge- plus the first four characters of VENDOR.
# metaview -> ge-meta, carta -> ge-cart, greenhouse -> ge-gree.
export SECRET_PREFIX="ge-${VENDOR:0:4}"
```

> [!NOTE]
> If you intend to publish the service to the Agent Registry catalog, register the
> entry in the same region as the Cloud Run service. See
> [Agent Registry](AGENT_REGISTRY.md).

### 1. Enable the required APIs

```bash
gcloud services enable \
    run.googleapis.com \
    secretmanager.googleapis.com \
    cloudtrace.googleapis.com \
    telemetry.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    --project="${PROJECT_ID}"
```

### 2. Create the runtime service account

The proxy runs as a dedicated identity rather than the default Compute service
account, scoping its permissions strictly to this workload.

```bash
gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="GE ${VENDOR} MCP Proxy SA" \
    --project="${PROJECT_ID}"
```

### 3. Grant least-privilege IAM

The proxy creates, reads, and deletes secrets for OAuth sessions, authorization codes, and user tokens. It needs secret *lifecycle* permissions.

> [!CAUTION]
> Do not use `roles/secretmanager.admin` here. It additionally grants
> `secretmanager.secrets.setIamPolicy`, which would allow a compromised container to
> grant itself access to all secrets in the project. The custom role below contains
> exactly the verbs the application requires.

```bash
gcloud iam roles create geminiMcpProxyTokenStore \
    --project="${PROJECT_ID}" \
    --title="Gemini Enterprise MCP Proxy Token Store" \
    --description="Create, read and destroy per-user OAuth token secrets" \
    --stage=GA \
    --permissions=secretmanager.secrets.create,secretmanager.secrets.delete,secretmanager.secrets.get,secretmanager.secrets.list,secretmanager.versions.add,secretmanager.versions.access,secretmanager.versions.destroy,secretmanager.versions.get,secretmanager.versions.list

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="projects/${PROJECT_ID}/roles/geminiMcpProxyTokenStore" \
    --condition=None

# Required to export OpenTelemetry spans to Cloud Trace.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/cloudtrace.agent" \
    --condition=None
```

### 4. Store static credentials in Secret Manager

Two long-lived credentials are needed: the client secret Gemini Enterprise presents to this proxy, and the upstream client secret (if using a confidential client).

```bash
# Client secret for Gemini Enterprise to authenticate against this proxy
printf '%s' "$(openssl rand -hex 24)" | gcloud secrets create "ge-${VENDOR}-client-secret" \
    --data-file=- --replication-policy=automatic --project="${PROJECT_ID}"

# Upstream client secret (leave empty for public PKCE clients like Metaview)
printf '%s' "${UPSTREAM_CLIENT_SECRET:-}" | gcloud secrets create "${VENDOR}-client-secret" \
    --data-file=- --replication-policy=automatic --project="${PROJECT_ID}"
```

To rotate later, add a new version rather than recreating the secret:

```bash
printf '%s' "new-value" | gcloud secrets versions add "ge-${VENDOR}-client-secret" --data-file=-
```

### 5. Deploy to Cloud Run

Credentials are mounted via `--set-secrets` rather than `--set-env-vars`.

> [!CAUTION]
> Never pass a credential via `--set-env-vars`. Plaintext environment variables are
> visible to anyone with `run.services.get` through `gcloud run services describe`,
> the Cloud Console, and revision history. `--set-secrets` stores only a reference;
> Cloud Run resolves the value securely at container startup.

```bash
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

# Upstream endpoints for the chosen vendor. Values below were read from each
# provider's own OAuth discovery documents; see deploy.sh for the full presets.
#
#   metaview    auth   https://auth.metaview.ai/oauth2/authorize
#               token  https://auth.metaview.ai/oauth2/token
#               mcp    https://mcp.metaview.ai/mcp
#               dcr    https://auth.metaview.ai/oauth2/register
#               scopes openid profile email offline_access
#   carta       auth   https://mcp.app.carta.com/authorize
#               token  https://mcp.app.carta.com/token
#               mcp    https://mcp.app.carta.com/mcp
#               dcr    https://mcp.app.carta.com/register
#               scopes openid cuid read_mcp_firms read_mcp_companies read_mcp_crm
#   greenhouse  auth   https://auth.greenhouse.io/authorize
#               token  https://auth.greenhouse.io/token
#               mcp    https://mcp.greenhouse.io/mcp
#               dcr    https://mcp.greenhouse.io/oauth/register
#               scopes greenhouse:mcp_client   (offline_access is not accepted)
export UPSTREAM_AUTH_URL="https://auth.metaview.ai/oauth2/authorize"
export UPSTREAM_TOKEN_URL="https://auth.metaview.ai/oauth2/token"
export UPSTREAM_MCP_URL="https://mcp.metaview.ai/mcp"
export UPSTREAM_REGISTRATION_URL="https://auth.metaview.ai/oauth2/register"

gcloud run deploy "${SERVICE_NAME}" \
    --source . \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --service-account="${SA_EMAIL}" \
    --allow-unauthenticated \
    --set-env-vars="STORAGE_BACKEND=secret_manager" \
    --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID}" \
    --set-env-vars="GCP_PROJECT_NUMBER=${PROJECT_NUMBER}" \
    --set-env-vars="GCP_REGION=${REGION}" \
    --set-env-vars="SECRET_PREFIX=${SECRET_PREFIX}" \
    --set-env-vars="UPSTREAM_SERVICE_NAME=${VENDOR}" \
    --set-env-vars="UPSTREAM_AUTH_URL=${UPSTREAM_AUTH_URL}" \
    --set-env-vars="UPSTREAM_TOKEN_URL=${UPSTREAM_TOKEN_URL}" \
    --set-env-vars="UPSTREAM_MCP_URL=${UPSTREAM_MCP_URL}" \
    --set-env-vars="UPSTREAM_RESOURCE=${UPSTREAM_MCP_URL}" \
    --set-env-vars="GE_CLIENT_ID=ge-${VENDOR}-client" \
    --set-env-vars="GE_ALLOWED_REDIRECT_URIS=https://vertexaisearch.cloud.google.com/oauth-redirect" \
    --set-env-vars="UPSTREAM_CLIENT_ID=${UPSTREAM_CLIENT_ID:-placeholder}" \
    --set-secrets="GE_CLIENT_SECRET=ge-${VENDOR}-client-secret:latest"
```

> [!IMPORTANT]
> The application refuses to start unless `UPSTREAM_CLIENT_ID`, `UPSTREAM_AUTH_URL`,
> `UPSTREAM_TOKEN_URL`, `UPSTREAM_MCP_URL` and `GE_CLIENT_SECRET` all have values
> (the check is skipped only when `STORAGE_BACKEND=memory`, i.e. in tests). None of
> them has a vendor default any more.
>
> That is why the first deploy passes `UPSTREAM_CLIENT_ID=${UPSTREAM_CLIENT_ID:-placeholder}`:
> the real client ID does not exist until Dynamic Client Registration runs in step 6,
> which needs the service URL that only exists after this deploy. Step 7 replaces the
> placeholder. Sign-in will fail until it does.

> [!IMPORTANT]
> The command above deliberately does **not** mount `UPSTREAM_CLIENT_SECRET`.
> Providers onboarded through RFC 7591 Dynamic Client Registration are registered as
> *public* clients (`token_endpoint_auth_method=none`) and have no secret at all.
> Carta goes further and advertises only `none` and `private_key_jwt`, so a secret is
> never valid there.
>
> Two traps follow from this:
>
> 1. `gcloud secrets create --data-file=-` with an empty payload creates the secret
>    container but **silently skips creating version 1**. Mounting `:latest` then fails
>    with `Secret projects/.../versions/latest was not found` and the deploy aborts.
> 2. Mounting a placeholder value instead would send a bogus `client_secret` upstream
>    and break the token exchange. `app/oauth/client.py` omits the parameter entirely
>    when the value is empty, which is what a public PKCE client must do.
>
> Only add the mount when the provider actually issued you a confidential client
> secret:
>
> ```bash
>     --set-secrets="UPSTREAM_CLIENT_SECRET=${VENDOR}-client-secret:latest"
> ```

> [!NOTE]
> `--allow-unauthenticated` is required because user browsers must reach
> `/oauth/authorize` and `/oauth/callback` during sign-in. Per-user authorization is
> enforced inside the application via the proxy bearer token, not by Cloud Run IAM.

Capture the assigned URL:

```bash
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" --project="${PROJECT_ID}" \
    --format='value(status.url)')
echo "${SERVICE_URL}"
```

### 6. Dynamic Client Registration (RFC 7591)

If the provider supports RFC 7591 Dynamic Client Registration, register the newly assigned Cloud Run callback URI against that provider's registration endpoint (`UPSTREAM_REGISTRATION_URL`, set in step 5):

```bash
curl -fsS -X POST "${UPSTREAM_REGISTRATION_URL}" \
    -H "Content-Type: application/json" \
    -d "{
      \"client_name\": \"Gemini Enterprise ${VENDOR} Proxy\",
      \"token_endpoint_auth_method\": \"none\",
      \"grant_types\": [\"authorization_code\", \"refresh_token\"],
      \"response_types\": [\"code\"],
      \"redirect_uris\": [\"${SERVICE_URL}/oauth/callback\"]
    }"
```

Extract `client_id` from the JSON response as your `UPSTREAM_CLIENT_ID`.

*(Alternatively, run `python3 -m app.oauth.dcr --registration-url "${UPSTREAM_REGISTRATION_URL}" --redirect-uri "${SERVICE_URL}/oauth/callback"`).*

### 7. Apply the resolved public URL and client ID

```bash
gcloud run services update "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --update-env-vars="PROXY_BASE_URL=${SERVICE_URL},UPSTREAM_CLIENT_ID=${UPSTREAM_CLIENT_ID}"
```

### 8. Verify

```bash
curl -s "${SERVICE_URL}/health" | jq
curl -s "${SERVICE_URL}/.well-known/oauth-authorization-server" | jq
```

Confirm the test console is **not** exposed — both must return `404`:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "${SERVICE_URL}/test"
curl -s -o /dev/null -w '%{http_code}\n' "${SERVICE_URL}/test/active-token"
```

Confirm an invalid token is rejected — must return `401`:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST "${SERVICE_URL}/mcp" \
    -H "Authorization: Bearer not-a-real-token" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## Connecting Gemini Enterprise

Add the broker as a **custom MCP** connector in the Gemini Enterprise console.
Gemini Enterprise then talks to Cloud Run directly, with the broker acting as its
OAuth 2.0 authorization server.

> [!NOTE]
> Publishing the service to the Agent Registry catalog is optional and does not
> change this configuration or the traffic path. See
> [Agent Registry](AGENT_REGISTRY.md).

Retrieve the client secret:

```bash
gcloud secrets versions access latest --secret="ge-${VENDOR}-client-secret" --project="${PROJECT_ID}"
```

Then configure the Gemini Enterprise console:

| Field | Value |
|---|---|
| MCP Server URL | `${SERVICE_URL}/mcp` |
| Authentication | OAuth 2.0 |
| Client ID | `ge-${VENDOR}-client` |
| Client Secret | Value retrieved above |
| Authorization URL | `${SERVICE_URL}/oauth/authorize` |
| Token URL | `${SERVICE_URL}/oauth/token` |
| Scopes | `offline_access` |
| Redirect URI | `https://vertexaisearch.cloud.google.com/oauth-redirect` |
| Enable PKCE Support | **Yes** |

> [!NOTE]
> Enable PKCE. The broker advertises `S256` only and verifies the `code_verifier` at
> the token endpoint when a challenge was presented. The proxy-to-upstream leg always
> uses S256 regardless of this setting, so enabling it hardens the Gemini
> Enterprise-to-proxy hop specifically.
>
> The redirect URI must appear in `GE_ALLOWED_REDIRECT_URIS` or `/oauth/authorize`
> rejects the request with `400`. This is an exact-match allowlist.

---

## Rollback

```bash
gcloud run revisions list --service="${SERVICE_NAME}" --region="${REGION}"

gcloud run services update-traffic "${SERVICE_NAME}" \
    --region="${REGION}" \
    --to-revisions=PREVIOUS_REVISION=100
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Revision fails to start; logs show `Refusing to start: required settings are unset: ...` | One or more of `UPSTREAM_CLIENT_ID`, `UPSTREAM_AUTH_URL`, `UPSTREAM_TOKEN_URL`, `UPSTREAM_MCP_URL`, `GE_CLIENT_SECRET` is empty. There are no vendor defaults, and the check is bypassed only when `STORAGE_BACKEND=memory`. | Set the variable named in the message and redeploy: endpoints via `--set-env-vars`, `GE_CLIENT_SECRET` via `--set-secrets`. |
| `403` from Secret Manager at startup | Custom role not bound, or IAM propagation delay | Wait ~60s and retry; confirm the binding from step 3. |
| All `/mcp` calls return `401` | `PROXY_BASE_URL` not applied in step 7, so sign-in redirects never complete | Run step 7 with the real service URL. |
| Sign-in fails with invalid redirect URI | Step 6 not run, or run before the service URL existed | Re-run Dynamic Client Registration with `${SERVICE_URL}/oauth/callback`, then apply the returned client ID. |
| Traces missing in Cloud Trace | `roles/cloudtrace.agent` not granted, or `GCP_PROJECT_NUMBER` unset | Grant the role (step 3) and set `GCP_PROJECT_NUMBER`. |
| A newly set environment variable has no effect, but `gcloud run services describe` shows it configured | `--update-env-vars` sets the variable but does not ship new code. If the running image predates the setting, nothing reads it. | Redeploy the image with `gcloud run deploy --source .`, then verify by exercising the behaviour rather than by re-reading the config. |
| `/test` returns 404 | Expected. The test console is enabled for local development only. | No action. |
| `POST /oauth/token` returns `400 invalid_grant` with `PKCE verification failed` | Gemini Enterprise replayed a stale `code_challenge`. | Remove and re-add the connector to force a fresh PKCE pair. The broker logs the expected and computed challenge so you can confirm. |
