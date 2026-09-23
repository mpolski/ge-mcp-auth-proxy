#!/usr/bin/env bash
# ==============================================================================
# Deployment script for the Gemini Enterprise Universal MCP Identity Broker.
#
# Supports deploying isolated, vendor-separated Cloud Run instances for any
# Model Context Protocol (MCP) SaaS provider (Metaview, Carta, Greenhouse, etc.).
#
# This is a convenience wrapper. Every step below is a plain gcloud command and
# can be run by hand - see docs/DEPLOYMENT.md for the step-by-step runbook.
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# Vendor Profile & Configuration
# ------------------------------------------------------------------------------
# VENDOR selects a preset profile below, and names the Cloud Run service, the
# service account and the Secret Manager namespace. It is deliberately not
# defaulted: picking a vendor for the operator is how a deployment ends up
# pointed at somebody else's SaaS tenant.
VENDOR="${VENDOR:-${UPSTREAM_SERVICE_NAME:-}}"
if [[ -z "${VENDOR}" ]]; then
    cat >&2 <<'USAGE'
ERROR: VENDOR is not set.

Usage:  VENDOR=<name> ./deploy.sh

Preset profiles:  metaview | carta | greenhouse

Any other name deploys a generic profile; supply the endpoints yourself:

  VENDOR=acme \
  UPSTREAM_AUTH_URL=https://auth.acme.com/authorize \
  UPSTREAM_TOKEN_URL=https://auth.acme.com/token \
  UPSTREAM_MCP_URL=https://mcp.acme.com/mcp \
  ./deploy.sh

Discover those values from the provider itself:
  curl -s -D- -X POST https://<mcp-host>/mcp        # read WWW-Authenticate
  curl -s https://<mcp-host>/.well-known/oauth-protected-resource/mcp
  curl -s https://<auth-host>/.well-known/oauth-authorization-server
USAGE
    exit 1
fi
VENDOR="$(echo "${VENDOR}" | tr '[:upper:]' '[:lower:]')"

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${GCP_REGION:-us-central1}"

# Service naming (one Cloud Run service per vendor for failure domain isolation)
SERVICE_NAME="${SERVICE_NAME:-ge-${VENDOR}-proxy}"

SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-${VENDOR}-proxy-sa}"
CUSTOM_ROLE_ID="${CUSTOM_ROLE_ID:-geminiMcpProxyTokenStore}"

# Secret Manager secret names holding static configuration credentials
GE_SECRET_NAME="${GE_SECRET_NAME:-ge-${VENDOR}-client-secret}"
UPSTREAM_SECRET_NAME="${UPSTREAM_SECRET_NAME:-${VENDOR}-client-secret}"
SECRET_PREFIX="${SECRET_PREFIX:-ge-${VENDOR:0:4}}"

# Gemini Enterprise Client ID
GE_CLIENT_ID="${GE_CLIENT_ID:-ge-${VENDOR}-client}"
GE_CLIENT_SECRET="${GE_CLIENT_SECRET:-}"

# Exact-match allowlist of redirect URIs accepted at /oauth/authorize.
# Defaults to the fixed Gemini Enterprise callback. When using Agent Registry
# 3LO, append the Agent Identity auth manager callback, comma-separated:
#   https://iamconnectorcredentials.googleapis.com/v1/projects/<P>/locations/<L>/connectors/<C>/oauthcallback
GE_REDIRECT_URI="${GE_REDIRECT_URI:-https://vertexaisearch.cloud.google.com/oauth-redirect}"
GE_ALLOWED_REDIRECT_URIS="${GE_ALLOWED_REDIRECT_URIS:-${GE_REDIRECT_URI}}"

# ------------------------------------------------------------------------------
# Vendor Preset Defaults
#
# Every URL below was read from the provider's own discovery documents on
# 2026-09-23, not from vendor marketing pages or assumption. To re-verify any
# profile, ask the MCP endpoint itself:
#
#   curl -s https://<mcp-host>/.well-known/oauth-protected-resource/mcp
#   curl -s https://<auth-host>/.well-known/oauth-authorization-server
#
# All three providers advertise RFC 7591 Dynamic Client Registration and
# code_challenge_methods_supported=["S256"], which is precisely the combination
# Gemini Enterprise cannot satisfy on its own and this broker exists to bridge.
# ------------------------------------------------------------------------------
case "${VENDOR}" in
    metaview)
        # Tested end-to-end against a live Gemini Enterprise connector
        # on 2026-09-23. Carta and Greenhouse are configured from their
        # published discovery documents but have not been run end-to-end.
        DEFAULT_AUTH_URL="https://auth.metaview.ai/oauth2/authorize"
        DEFAULT_TOKEN_URL="https://auth.metaview.ai/oauth2/token"
        DEFAULT_MCP_URL="https://mcp.metaview.ai/mcp"
        DEFAULT_SCOPES="openid profile email offline_access"
        DEFAULT_RESOURCE="https://mcp.metaview.ai/mcp"
        DEFAULT_AUDIENCE=""
        DEFAULT_REG_URL="https://auth.metaview.ai/oauth2/register"
        ;;
    carta)
        # Carta CRM MCP server. The authorization server is the MCP host itself
        # (issuer "https://mcp.app.carta.com/"), not a separate auth subdomain.
        #
        # token_endpoint_auth_methods_supported is ["none", "private_key_jwt"]:
        # there is NO client_secret option, so this profile must be deployed as a
        # public PKCE client. Leave UPSTREAM_CLIENT_SECRET unset.
        #
        # Only read scopes are requested. The readwrite_* variants exist and are
        # deliberately omitted so a misbehaving agent cannot mutate CRM records.
        DEFAULT_AUTH_URL="https://mcp.app.carta.com/authorize"
        DEFAULT_TOKEN_URL="https://mcp.app.carta.com/token"
        DEFAULT_MCP_URL="https://mcp.app.carta.com/mcp"
        DEFAULT_SCOPES="openid cuid read_mcp_firms read_mcp_companies read_mcp_crm"
        DEFAULT_RESOURCE="https://mcp.app.carta.com/mcp"
        DEFAULT_AUDIENCE=""
        DEFAULT_REG_URL="https://mcp.app.carta.com/register"
        ;;
    greenhouse)
        # Greenhouse MCP server. Note the split: the resource lives on
        # mcp.greenhouse.io but the authorization server is auth.greenhouse.io,
        # while DCR is served from the MCP host. api.greenhouse.io does NOT serve
        # these OAuth endpoints.
        #
        # greenhouse:mcp_client is the only scope the server advertises;
        # offline_access is not accepted even though refresh_token is a supported
        # grant type.
        DEFAULT_AUTH_URL="https://auth.greenhouse.io/authorize"
        DEFAULT_TOKEN_URL="https://auth.greenhouse.io/token"
        DEFAULT_MCP_URL="https://mcp.greenhouse.io/mcp"
        DEFAULT_SCOPES="greenhouse:mcp_client"
        DEFAULT_RESOURCE="https://mcp.greenhouse.io/mcp"
        DEFAULT_AUDIENCE=""
        DEFAULT_REG_URL="https://mcp.greenhouse.io/oauth/register"
        ;;
    *)
        DEFAULT_AUTH_URL=""
        DEFAULT_TOKEN_URL=""
        DEFAULT_MCP_URL=""
        DEFAULT_SCOPES="openid offline_access"
        DEFAULT_RESOURCE=""
        DEFAULT_AUDIENCE=""
        DEFAULT_REG_URL=""
        ;;
esac

UPSTREAM_CLIENT_ID="${UPSTREAM_CLIENT_ID:-}"
UPSTREAM_CLIENT_SECRET="${UPSTREAM_CLIENT_SECRET:-}"
UPSTREAM_AUTH_URL="${UPSTREAM_AUTH_URL:-${DEFAULT_AUTH_URL}}"
UPSTREAM_TOKEN_URL="${UPSTREAM_TOKEN_URL:-${DEFAULT_TOKEN_URL}}"
UPSTREAM_MCP_URL="${UPSTREAM_MCP_URL:-${DEFAULT_MCP_URL}}"
UPSTREAM_SCOPES="${UPSTREAM_SCOPES:-${DEFAULT_SCOPES}}"
UPSTREAM_RESOURCE="${UPSTREAM_RESOURCE:-${DEFAULT_RESOURCE}}"
UPSTREAM_AUDIENCE="${UPSTREAM_AUDIENCE:-${DEFAULT_AUDIENCE}}"
UPSTREAM_REGISTRATION_URL="${UPSTREAM_REGISTRATION_URL:-${DEFAULT_REG_URL}}"

# Fail before creating any cloud resources rather than after. A service account,
# custom role and several secrets are created below; a revision that cannot
# reach an upstream would leave all of them behind.
missing_endpoints=()
if [[ -z "${UPSTREAM_AUTH_URL}" ]]; then missing_endpoints+=("UPSTREAM_AUTH_URL"); fi
if [[ -z "${UPSTREAM_TOKEN_URL}" ]]; then missing_endpoints+=("UPSTREAM_TOKEN_URL"); fi
if [[ -z "${UPSTREAM_MCP_URL}" ]]; then missing_endpoints+=("UPSTREAM_MCP_URL"); fi
if [[ ${#missing_endpoints[@]} -gt 0 ]]; then
    echo "ERROR: no preset profile for VENDOR='${VENDOR}' and these are unset: ${missing_endpoints[*]}" >&2
    echo "Set them in the environment, or use a preset: metaview | carta | greenhouse" >&2
    exit 1
fi

if [[ -z "${PROJECT_ID}" ]]; then
    echo "ERROR: GCP_PROJECT_ID is not set and no default gcloud project was found." >&2
    echo "Run: gcloud config set project <YOUR_PROJECT_ID>" >&2
    exit 1
fi

SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "============================================================"
echo " Deploying Gemini Enterprise MCP Proxy for: ${VENDOR^^}"
echo " Project: ${PROJECT_ID}"
echo " Region:  ${REGION}"
echo " Service: ${SERVICE_NAME}"
echo " Namespace: ${SECRET_PREFIX}"
echo "============================================================"

# If client ID is empty and no DCR URL is configured, ask for it
if [[ -z "${UPSTREAM_CLIENT_ID}" && -z "${UPSTREAM_REGISTRATION_URL}" ]]; then
    read -rp "Enter ${VENDOR} OAuth Client ID: " UPSTREAM_CLIENT_ID
fi

# ------------------------------------------------------------------------------
# 1. Enable required APIs
# ------------------------------------------------------------------------------
echo ""
echo "[1/7] Enabling required Google Cloud APIs..."
gcloud services enable \
    run.googleapis.com \
    secretmanager.googleapis.com \
    cloudtrace.googleapis.com \
    telemetry.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    --project="${PROJECT_ID}"

# ------------------------------------------------------------------------------
# 2. Create the runtime service account
# ------------------------------------------------------------------------------
echo ""
echo "[2/7] Creating service account ${SA_EMAIL}..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
        --display-name="GE ${VENDOR^^} MCP Proxy SA" \
        --project="${PROJECT_ID}"
else
    echo "      Already exists, skipping."
fi

# ------------------------------------------------------------------------------
# 3. Grant least-privilege IAM
# ------------------------------------------------------------------------------
echo ""
echo "[3/7] Configuring least-privilege IAM role..."
if ! gcloud iam roles describe "${CUSTOM_ROLE_ID}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud iam roles create "${CUSTOM_ROLE_ID}" \
        --project="${PROJECT_ID}" \
        --title="Gemini Enterprise MCP Proxy Token Store" \
        --description="Create, read and destroy per-user OAuth token secrets" \
        --stage=GA \
        --permissions=\
secretmanager.secrets.create,\
secretmanager.secrets.delete,\
secretmanager.secrets.get,\
secretmanager.secrets.list,\
secretmanager.versions.add,\
secretmanager.versions.access,\
secretmanager.versions.destroy,\
secretmanager.versions.get,\
secretmanager.versions.list
else
    echo "      Role '${CUSTOM_ROLE_ID}' already exists, skipping."
fi

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="projects/${PROJECT_ID}/roles/${CUSTOM_ROLE_ID}" \
    --condition=None \
    --quiet >/dev/null

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/cloudtrace.agent" \
    --condition=None \
    --quiet >/dev/null

# ------------------------------------------------------------------------------
# 4. Store static credentials in Secret Manager
# ------------------------------------------------------------------------------
echo ""
echo "[4/7] Storing static credentials in Secret Manager..."

# NOTE: 'gcloud secrets create --data-file=-' with an EMPTY payload creates the
# secret container but silently skips creating version 1. A later
# '--set-secrets=FOO=name:latest' then fails with "versions/latest was not
# found". Never call this helper with an empty value.
create_or_update_secret() {
    local name="$1" value="$2"
    if [[ -z "${value}" ]]; then
        echo "      ERROR: refusing to write an empty value to secret '${name}'." >&2
        echo "      Secret Manager would create a version-less secret that Cloud Run cannot mount." >&2
        exit 1
    fi
    if gcloud secrets describe "${name}" --project="${PROJECT_ID}" &>/dev/null; then
        printf '%s' "${value}" | gcloud secrets versions add "${name}" \
            --data-file=- --project="${PROJECT_ID}" >/dev/null
        echo "      Updated ${name}"
    else
        printf '%s' "${value}" | gcloud secrets create "${name}" \
            --data-file=- --replication-policy=automatic \
            --project="${PROJECT_ID}" >/dev/null
        echo "      Created ${name}"
    fi
}

# Returns 0 only if the secret exists AND has a resolvable 'latest' version.
secret_has_version() {
    gcloud secrets versions describe latest \
        --secret="$1" --project="${PROJECT_ID}" &>/dev/null
}

if [[ -n "${GE_CLIENT_SECRET:-}" ]]; then
    create_or_update_secret "${GE_SECRET_NAME}" "${GE_CLIENT_SECRET}"
elif secret_has_version "${GE_SECRET_NAME}"; then
    echo "      Reusing existing ${GE_SECRET_NAME}"
else
    create_or_update_secret "${GE_SECRET_NAME}" "$(openssl rand -hex 24)"
fi

# The upstream client secret is OPTIONAL. Providers onboarded through RFC 7591
# Dynamic Client Registration are registered as PUBLIC clients
# (token_endpoint_auth_method=none) and have no secret at all. In that case we
# deliberately do NOT mount the env var: app/oauth/client.py omits the
# 'client_secret' parameter entirely when it is empty, which is what a public
# PKCE client must do. Mounting a placeholder would send a bogus secret upstream
# and break the token exchange.
UPSTREAM_SECRET_MOUNT=""
if [[ -n "${UPSTREAM_CLIENT_SECRET:-}" ]]; then
    create_or_update_secret "${UPSTREAM_SECRET_NAME}" "${UPSTREAM_CLIENT_SECRET}"
    UPSTREAM_SECRET_MOUNT="UPSTREAM_CLIENT_SECRET=${UPSTREAM_SECRET_NAME}:latest"
elif secret_has_version "${UPSTREAM_SECRET_NAME}"; then
    echo "      Reusing existing ${UPSTREAM_SECRET_NAME}"
    UPSTREAM_SECRET_MOUNT="UPSTREAM_CLIENT_SECRET=${UPSTREAM_SECRET_NAME}:latest"
else
    echo "      No upstream client secret supplied."
    echo "      Deploying as a PUBLIC (PKCE) client - correct for RFC 7591 DCR providers."
fi

# Assemble the secret mounts for the deploy step.
SECRET_MOUNTS="GE_CLIENT_SECRET=${GE_SECRET_NAME}:latest"
if [[ -n "${UPSTREAM_SECRET_MOUNT}" ]]; then
    SECRET_MOUNTS="${SECRET_MOUNTS},${UPSTREAM_SECRET_MOUNT}"
fi

# ------------------------------------------------------------------------------
# 5. Deploy to Cloud Run
# ------------------------------------------------------------------------------
echo ""
echo "[5/7] Deploying to Cloud Run..."
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

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
    --set-env-vars="UPSTREAM_SCOPES=${UPSTREAM_SCOPES}" \
    --set-env-vars="UPSTREAM_RESOURCE=${UPSTREAM_RESOURCE}" \
    --set-env-vars="UPSTREAM_AUDIENCE=${UPSTREAM_AUDIENCE}" \
    --set-env-vars="GE_CLIENT_ID=${GE_CLIENT_ID}" \
    --set-env-vars="GE_ALLOWED_REDIRECT_URIS=${GE_ALLOWED_REDIRECT_URIS}" \
    --set-env-vars="UPSTREAM_CLIENT_ID=${UPSTREAM_CLIENT_ID:-placeholder}" \
    --set-secrets="${SECRET_MOUNTS}" \
    --quiet

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format='value(status.url)')

# ------------------------------------------------------------------------------
# 6. Dynamic Client Registration (RFC 7591)
# ------------------------------------------------------------------------------
if [[ -n "${UPSTREAM_REGISTRATION_URL}" && -z "${UPSTREAM_CLIENT_ID}" ]]; then
    echo ""
    echo "[6/7] Registering redirect URI with ${VENDOR} via RFC 7591 DCR..."
    DCR_RESP=$(curl -fsS -X POST "${UPSTREAM_REGISTRATION_URL}" \
        -H "Content-Type: application/json" \
        -d "{
          \"client_name\": \"Gemini Enterprise ${VENDOR} Proxy\",
          \"token_endpoint_auth_method\": \"none\",
          \"grant_types\": [\"authorization_code\", \"refresh_token\"],
          \"response_types\": [\"code\"],
          \"redirect_uris\": [\"${SERVICE_URL}/oauth/callback\"]
        }" 2>/dev/null) || {
            echo "      WARNING: Dynamic client registration failed or not supported." >&2
            echo "      Keeping UPSTREAM_CLIENT_ID=${UPSTREAM_CLIENT_ID}." >&2
            echo "      Ensure ${SERVICE_URL}/oauth/callback is registered in your provider portal." >&2
            DCR_RESP=""
        }

    if [[ -n "${DCR_RESP}" ]]; then
        REGISTERED_CLIENT_ID=$(echo "${DCR_RESP}" | python3 -c \
            "import sys, json; print(json.load(sys.stdin).get('client_id', ''))" 2>/dev/null || true)
        if [[ -n "${REGISTERED_CLIENT_ID}" ]]; then
            UPSTREAM_CLIENT_ID="${REGISTERED_CLIENT_ID}"
            echo "      Registered as ${UPSTREAM_CLIENT_ID}"
        fi
    fi
else
    echo ""
    echo "[6/7] Skipping Dynamic Client Registration (${VENDOR} client ID already configured)."
fi

# ------------------------------------------------------------------------------
# 7. Apply the resolved public URL and client ID
# ------------------------------------------------------------------------------
echo ""
echo "[7/7] Applying PROXY_BASE_URL and UPSTREAM_CLIENT_ID..."
gcloud run services update "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --update-env-vars="PROXY_BASE_URL=${SERVICE_URL},UPSTREAM_CLIENT_ID=${UPSTREAM_CLIENT_ID}" \
    --quiet >/dev/null

echo ""
echo "============================================================"
echo " DEPLOYMENT SUCCESSFUL: ${VENDOR^^} PROXY"
echo "============================================================"
echo "Service URL:  ${SERVICE_URL}"
echo "Health check: ${SERVICE_URL}/health"
echo ""
echo "--- GEMINI ENTERPRISE CONSOLE CONFIGURATION ---"
echo "MCP Server URL:    ${SERVICE_URL}/mcp"
echo "Authentication:    OAuth 2.0"
echo "Client ID:         ${GE_CLIENT_ID}"
echo "Authorization URL: ${SERVICE_URL}/oauth/authorize"
echo "Token URL:         ${SERVICE_URL}/oauth/token"
echo "Scopes:            offline_access"
echo "Redirect URI:      ${GE_REDIRECT_URI}"
echo ""
echo "Allowed redirect URIs (enforced at /oauth/authorize):"
echo "                   ${GE_ALLOWED_REDIRECT_URIS}"
echo ""
echo "Client Secret:     stored in Secret Manager, not printed here."
echo "                   Retrieve it with:"
echo "                     gcloud secrets versions access latest \\"
echo "                       --secret=${GE_SECRET_NAME} --project=${PROJECT_ID}"
echo "============================================================"
