# Agent Registry

Related: [Architecture](ARCHITECTURE.md) · [Deployment](DEPLOYMENT.md) · [Security](SECURITY.md) · [Testing](TESTING.md)

---

## What this is for

Registering the broker in Agent Registry makes it **discoverable in the Gemini
Enterprise tool catalog**. That is the whole scope here.

Registration is **optional**. Gemini Enterprise connects to the broker over a plain
HTTPS MCP URL, and that is the supported path. Registering does not change how
traffic flows, how users authenticate, or how per-user isolation works.

> [!NOTE]
> Routing traffic through **Agent Gateway** is deliberately out of scope. It needs a
> gateway, a binding, an Agent Identity connector and an IAP egress grant, and it
> changes which credential reaches the broker.

---

## 1. Prerequisites

- The broker deployed and healthy (`curl $SERVICE_URL/health`).
- `gcloud` authenticated against the target project.

```bash
export PROJECT_ID="<YOUR_PROJECT_ID>"
export REGION="us-central1"
export SERVICE_URL="https://ge-<vendor>-proxy-<hash>.<region>.run.app"
```

---

## 2. Register the MCP server

The tool manifest lives in [`toolspec.json`](../toolspec.json) at the repo root.

```bash
gcloud agent-registry services create mcp-identity-broker \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --display-name="MCP Identity Broker" \
  --description="Per-user OAuth MCP broker. Read-only tool allowlist." \
  --interfaces="protocolBinding=JSONRPC,url=${SERVICE_URL}/mcp" \
  --mcp-server-spec-content=toolspec.json \
  --mcp-server-spec-type=TOOL_SPEC
```

> [!WARNING]
> Two traps in the official examples, both hit during development:
>
> 1. `--mcp-server-spec-content=@toolspec.json` — the `@` prefix is **wrong** and
>    fails. Pass the bare path.
> 2. The published sample omits `inputSchema` on each tool, but it is **required**.
>    A tool without it is rejected.

Verify:

```bash
gcloud agent-registry services describe mcp-identity-broker \
  --location="${REGION}" --project="${PROJECT_ID}"
```

A successful entry has an `interfaces[].url` pointing at your `/mcp` endpoint and a
populated `mcpServerSpec.content.tools` array.

---

## 3. Registry location

Register in the **same region as the Cloud Run service**, plus `global` if you want
the entry to appear alongside globally-scoped Gemini Enterprise apps.

| Location | Services | Notes |
|---|---|---|
| `global` | Yes | Matches a global Gemini Enterprise app |
| `us-central1` | Yes | Matches a `us-central1` Cloud Run service |
| `us` | **No** | `location is not supported` |

---

## 4. The toolspec is an allowlist, and that is the point

`toolspec.json` declares **only read-only tools**. Every mutating tool the upstream
exposes is deliberately absent.

This matters because the broker forwards whatever JSON-RPC body it receives. The
toolspec governs what Gemini Enterprise will *offer the model*, not what the
upstream will *accept*. It is a usability and governance control, not an
enforcement boundary.

> [!IMPORTANT]
> If you need hard enforcement, the check belongs in
> [`app/mcp/proxy.py`](../app/mcp/proxy.py), where the request body can be
> inspected before forwarding. Not currently implemented.

---

## 5. Keeping the toolspec honest

Tool names and schemas in `toolspec.json` were authored from vendor documentation,
not from a live handshake. The `inputSchema` entries are permissive
(`additionalProperties: true`, no declared properties).

To regenerate from the live server, authorize once and then call `tools/list`
through the broker with a valid proxy access token. Replace the placeholder schemas
with what the upstream actually returns.

An over-permissive schema is the safer failure direction: the model may pass an
argument the upstream rejects, which surfaces as a clean tool error rather than a
silently wrong result.
