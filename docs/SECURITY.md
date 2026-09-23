# Security Model

How per-user isolation is enforced, what was fixed, and what is still open.

Related: [Architecture](ARCHITECTURE.md) · [Deployment](DEPLOYMENT.md) · [Testing](TESTING.md)

---

## Threat model

This proxy mediates access to data that is permissioned per user inside the upstream
SaaS provider — for example interview transcripts, candidate evaluations and hiring
deliberations in Metaview. The central security requirement is therefore:

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

## Resolved issues

The following were identified and fixed. Each has a regression test.

### Cross-user token fallback in the MCP path

**Severity: critical — authentication bypass.**

`_resolve_request_user_token` fell back to `get_latest_user_token()` when a bearer
token could not be resolved:

```python
allow_fallback = primary_token.startswith("eyJ") or (not is_testing)
```

`TESTING` is unset in production, so `not is_testing` was always true and the fallback
was **unconditional**. Any bearer string — including a randomly guessed one — was
served using the most recently active user's Metaview credentials.

This reproduced exactly the shared-static-credential anti-pattern the proxy exists to
prevent.

**Fixed:** the fallback is removed; unresolved tokens receive `401`.
**Tests:** `test_mcp_unknown_jwt_is_rejected_not_mapped_to_another_user`,
`test_mcp_garbage_token_does_not_borrow_active_session`.

### Cross-user fallback in the refresh grant

**Severity: high.**

`POST /oauth/token` with `grant_type=refresh_token` fell back to
`get_latest_user_token()` when the supplied refresh token was unknown, minting a valid
proxy access token bound to a different employee's session.

**Fixed:** unknown refresh tokens receive `invalid_grant`.
**Test:** `test_oauth_refresh_grant_rejects_unknown_refresh_token`.

### Client secret served in HTML

**Severity: critical — unauthenticated credential disclosure.**

`/test-callback` rendered `GE_CLIENT_SECRET` into the page returned to any caller:

```python
.replace("__GE_CLIENT_SECRET__", settings.GE_CLIENT_SECRET or "")
```

Combined with `/test/active-token` — also unauthenticated, returning a live proxy
access token — and a service deployed `--allow-unauthenticated`, this formed a
complete anonymous path to a real user's Metaview data:

```
GET /test-callback    → harvest GE_CLIENT_SECRET
GET /test/active-token → obtain a live proxy access token
POST /mcp             → read that user's interview data
```

**Fixed:** the token exchange moved server-side to `/test/exchange`, so the secret is
never rendered. The console is no longer mounted by default (below).

### Test console exposed in production

**Severity: critical.**

The console router was mounted unconditionally.

**Fixed:** mounted only when `ENABLE_TEST_CONSOLE=true`, which defaults to `false`.
Each route additionally calls `_require_console_enabled()` as defence in depth.
**Test:** `test_test_console_not_mounted_by_default`.

### Redirect URI was not validated

**Severity: high.**

`/oauth/authorize` accepted any `redirect_uri` the caller supplied, stored it on the
session, and redirected the browser back to it after upstream consent. That makes the
broker an open redirector and delivers the authorization code to an attacker-named
host. It was *mitigated* but not closed by `/oauth/token` requiring `GE_CLIENT_SECRET`,
so a stolen code could not be redeemed without the secret.

**Fixed:** `GE_ALLOWED_REDIRECT_URIS` holds an exact-match allowlist, defaulting to the
fixed Gemini Enterprise callback. Comparison is a simple string match as required by
RFC 6749 section 3.1.2.3 — deliberately no prefix, suffix or wildcard host matching,
which are the usual sources of redirect bypasses. Rejections return a direct `400`
rather than a redirect, because redirecting to an unvalidated URI would defeat the
check. Set `*` to disable enforcement in local development only.

**Tests:** `tests/test_redirect_allowlist.py` — covers the allow path, rejection,
absence of persisted session state on rejection, the multi-URI case, and five bypass
shapes: suffix-appended host, path suffix, path traversal, scheme downgrade, and the
`userinfo@host` trick.

> [!IMPORTANT]
> Changing `GE_ALLOWED_REDIRECT_URIS` on an existing Cloud Run service with
> `--update-env-vars` alone is **not** sufficient if the running image predates this
> control: the variable will be set but no code reads it, and `gcloud run services
> describe` will still show it as configured. Redeploy the image (`--source .`) and
> verify by sending a request with a bad `redirect_uri` and confirming a `400`.

### Over-broad IAM

The runtime service account held `roles/secretmanager.admin`, which includes
`secretmanager.secrets.setIamPolicy` — a compromised container could have granted
itself access to every secret in the project.

**Fixed:** replaced with a custom role containing only the verbs the code calls. See
[Deployment step 3](DEPLOYMENT.md#3-grant-least-privilege-iam).

### Secret version accumulation

**Severity: low security, high cost.**

`update_user_token` added a new secret version on every token refresh and nothing ever
destroyed the old ones, so superseded token material stayed live and readable (and
billable at $0.06/version/month) until the parent secret's 30-day TTL.

**Fixed:** `_sync_destroy_prior_versions` destroys superseded versions after each
write. Estimated saving at 100 users: **~$1,000/month**.
**Tests:** `test_update_user_token_destroys_superseded_versions`,
`test_pruning_failure_does_not_break_token_write`.

### Proxy access tokens never expired

**Severity: high.**

`POST /oauth/token` advertised `expires_in: 3600`, but nothing enforced it. Validation
at `/mcp` only asked whether a token record still existed, so a token's real lifetime
was the Secret Manager TTL on its secret — **30 days**.

**Fixed:** tokens now carry `proxy_expires_at`, stamped at mint and checked on every
request. Records written before that field existed fall back to
`created_at + PROXY_ACCESS_TOKEN_EXPIRES_IN`, so pre-existing tokens age out rather
than being treated as non-expiring.
**Tests:** `test_authorization_code_grant_stamps_expiry`,
`test_mcp_rejects_expired_proxy_access_token`,
`test_legacy_record_without_expiry_falls_back_to_created_at`.

### Refresh left every superseded token valid

**Severity: high.**

The refresh grant minted a replacement token and deliberately kept the previous one
alive. A user whose client refreshed hourly accumulated roughly **170 simultaneously
valid credentials** over a month, each usable for the full 30-day TTL.

**Fixed:** the superseded token is retired to a short grace window
(`PROXY_TOKEN_REVOCATION_GRACE_SECONDS`, default 60s) so in-flight requests survive the
rotation but the old credential dies shortly after. Refresh tokens additionally carry
an absolute ceiling anchored to the original sign-in
(`PROXY_REFRESH_TOKEN_EXPIRES_IN`, default 30 days), so a session cannot renew itself
indefinitely.
**Tests:** `test_refresh_retires_superseded_access_token`,
`test_refresh_grace_window_keeps_old_token_briefly`,
`test_refresh_succeeds_after_access_token_expires`,
`test_refresh_rejected_past_absolute_expiry`.

### No way to revoke a leaked token

**Severity: high.**

`delete_user_token` existed in every storage backend but was never called from
anywhere. A credential known to have leaked could only be withdrawn by deleting Secret
Manager secrets by hand — and deleting the obvious one was not enough, because a
session is stored twice (see [Architecture](ARCHITECTURE.md)).

**Fixed:** `POST /oauth/revoke` (RFC 7009), authenticated with the Gemini Enterprise
client credentials. It accepts either token type and tears down both the access-token
secret and the refresh-token index.
**Tests:** `test_revoke_access_token_kills_session`,
`test_revoke_refresh_token_kills_whole_session`,
`test_revoke_requires_client_authentication`,
`test_delete_user_token_also_removes_refresh_index`.

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

To revoke everything, delete the token secrets directly:

```bash
gcloud secrets list --filter="labels.app=metaview-mcp-proxy AND name~ge-mv-(tok|ref)-" \
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
