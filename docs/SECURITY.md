# Security Model

How per-user isolation is enforced, what was fixed, and what is still open.

Related: [Architecture](ARCHITECTURE.md) · [Deployment](DEPLOYMENT.md) · [Testing](TESTING.md)

---

## Threat model

This proxy mediates access to data that is permissioned per user inside the upstream
SaaS provider — for example interview transcripts, candidate evaluations and hiring
deliberations in Metaview, which is used throughout these docs as the worked example.
The central security requirement is therefore:

> A request carrying user A's credential must never return user B's data.

Everything below follows from that.

---

## Controls

1. **Per-user token isolation.** Each user's upstream token is stored under a distinct
   secret ID (`<prefix>-tok-<uuid>`). Requests are never multiplexed through a shared
   token. See [Architecture](ARCHITECTURE.md).
2. **Encrypted at rest.** Google Cloud Secret Manager, AES-256 by default, CMEK
   supported.
3. **Automated purging.** OAuth sessions (10 min) and authorization codes (5 min)
   expire via Secret Manager native TTL. Auth codes are additionally single-use and
   deleted on exchange.
4. **Credentials never in plaintext config.** `GE_CLIENT_SECRET` and
   `UPSTREAM_CLIENT_SECRET` are mounted with `--set-secrets`, not `--set-env-vars`.
5. **Least-privilege IAM.** A custom role grants exactly the Secret Manager verbs the
   code calls, and notably excludes `setIamPolicy`.
6. **PKCE.** S256 challenge on both legs of the flow.
7. **Audit logging.** Every secret version create, access and destroy is recorded in
   Cloud Audit Logs.

---

## Revoking a session

Use this when a proxy token is known or suspected to have leaked. It takes effect
immediately; the user simply signs in again.

```bash
curl -X POST "${SERVICE_URL}/oauth/revoke" \
  -u "${GE_CLIENT_ID}:${GE_CLIENT_SECRET}" \
  -d "token=${LEAKED_TOKEN}"
```

> [!NOTE]
> The endpoint returns `200` whether or not the token existed. That is required by
> RFC 7009 §2.2 — returning `404` for unknown tokens would turn it into an oracle for
> testing whether a guessed token is live.

To revoke everything, delete the token secrets directly. Every secret the broker
creates is labelled `app=ge-mcp-auth-proxy`, and secret IDs are namespaced by
`SECRET_PREFIX` — `ge-<first four characters of VENDOR>` when deployed with
`deploy.sh`, so `ge-meta` for Metaview, `ge-cart` for Carta, `ge-gree` for Greenhouse:

```bash
PREFIX="ge-meta"   # your SECRET_PREFIX
gcloud secrets list --filter="labels.app=ge-mcp-auth-proxy AND name~${PREFIX}-(tok|ref)-" \
  --format='value(name)' | xargs -r -n1 gcloud secrets delete --quiet
```


---

## Test console

> [!CAUTION]
> `ENABLE_TEST_CONSOLE` must remain `false` in every deployed environment.
>
> `/test/active-token` is unauthenticated **by design** and returns a working
> credential for whoever signed in most recently. It exists so a developer on
> localhost can resume a session without re-authenticating. On a reachable endpoint it
> is a full account-takeover primitive.

Verify it is absent after deploying:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "${SERVICE_URL}/test"              # expect 404
curl -s -o /dev/null -w '%{http_code}\n' "${SERVICE_URL}/test/active-token" # expect 404
```

---

## Enterprise Architecture Considerations

### The per-user credential mapping

Gemini Enterprise presents the user's proxy bearer token at `/mcp`, and
[_resolve_request_user_token](../app/mcp/proxy.py) maps it 1:1 to that user's upstream
token in Secret Manager. This is the only topology this release supports, and it is
the one the test suite exercises.

The resolver reads `X-Forwarded-Authorization` before `Authorization`, so an
intermediary that preserves the original end-user credential in the forwarded header
would also work unchanged.

> [!CAUTION]
> If you later introduce a proxy or gateway that presents only a **service identity**
> assertion rather than the end user's credential, the mapping breaks: there is no
> per-user credential left to resolve.
>
> Do not close that gap by falling back to "any active session". That was a real
> defect in this codebase (see
> [Cross-user token fallback](#cross-user-token-fallback-in-the-mcp-path)) and it
> collapses the per-user isolation the proxy exists to enforce. The correct fix is to
> verify the assertion, extract its identity claim, and index stored tokens by user
> identity — which the current schema does not do.

### Refresh tokens are accepted as bearer credentials at `/mcp`

`_resolve_request_user_token` falls back to resolving a request by refresh token when
the presented value is not a known access token. This is a deviation from OAuth, where
a refresh token should only ever be presented to the token endpoint.

It is bounded — the refresh token's own 30-day ceiling is enforced on that path — but
it does mean a leaked refresh token grants data access directly, without first being
exchanged. Removing it requires confirming that Gemini Enterprise never presents a
refresh token at the MCP endpoint.

### No rate limiting

`/oauth/token` and `/mcp` are unauthenticated at the network layer. Neither has
request throttling, so token-guessing is limited only by Secret Manager quota.
Consider Cloud Armor.

### Secret Manager write quota

Secret Manager permits **600 write requests per minute per project**, shared with
everything else in the project. At roughly 10 writes per sign-in this caps sustained
sign-ins at about 60/minute. Not a security issue, but it is a hard availability
ceiling worth knowing before a large rollout.
