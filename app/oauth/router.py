"""OAuth 2.0 Router for Gemini Enterprise broker endpoints."""

import base64
import logging
import time
import urllib.parse
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.oauth.client import exchange_code_for_tokens, refresh_access_token, UpstreamAuthError
from app.oauth.pkce import verify_code_challenge, generate_pkce_pair, compute_s256_challenge
from app.storage import (
    get_storage,
    StorageBackend,
    OAuthSessionData,
    AuthCodeData,
    UserTokenData,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/oauth", tags=["OAuth 2.0"])


def _extract_client_credentials(request: Request, form_data: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract client_id and client_secret from Authorization header (HTTP Basic) or Form body."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            encoded = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            if ":" in decoded:
                client_id, client_secret = decoded.split(":", 1)
                return client_id, client_secret
        except Exception as e:
            logger.warning("Failed to decode Basic Auth header: %s", e)

    client_id = form_data.get("client_id")
    client_secret = form_data.get("client_secret")
    return client_id, client_secret


def _authenticate_client(request: Request, form_dict: dict) -> Optional[JSONResponse]:
    """Verify Gemini Enterprise client credentials. Returns an error response, or None on success."""
    client_id, client_secret = _extract_client_credentials(request, form_dict)

    if client_id != settings.GE_CLIENT_ID:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_client", "error_description": "Invalid client_id"},
            headers={"WWW-Authenticate": "Basic"},
        )

    if settings.GE_CLIENT_SECRET and client_secret != settings.GE_CLIENT_SECRET:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_client", "error_description": "Invalid client_secret"},
            headers={"WWW-Authenticate": "Basic"},
        )

    return None


def _compute_s256_challenge(code_verifier: Optional[str]) -> str:
    """Render the S256 challenge for a verifier, for diagnostics only.

    Returns a placeholder rather than raising when there is no verifier, so that a
    logging call on an error path can never itself become the error.
    """
    if not code_verifier:
        return "<no code_verifier sent>"
    try:
        return compute_s256_challenge(code_verifier)
    except Exception:  # pragma: no cover - defensive, diagnostics must not raise
        return "<uncomputable>"


async def _revoke_session(storage: StorageBackend, user_token: UserTokenData) -> bool:
    """Tear down a user's proxy session. Best effort: never raises to the caller."""
    try:
        await storage.delete_user_token(user_token.proxy_access_token)
        return True
    except Exception as e:
        logger.warning("Failed to revoke proxy session: %s", e)
        return False


def _is_redirect_uri_allowed(redirect_uri: str) -> bool:
    """Check redirect_uri against the configured allowlist.

    RFC 6749 section 3.1.2.3 requires the authorization server to compare the
    redirect URI against a registered value using simple string comparison, so this
    is a deliberate exact match: no prefix, suffix or wildcard host matching, which
    are the usual sources of redirect bypasses.
    """
    if not settings.redirect_uri_enforcement_enabled:
        return True
    return redirect_uri in settings.allowed_redirect_uris


@router.get("/authorize")
async def oauth_authorize(
    client_id: str,
    redirect_uri: str,
    state: str,
    response_type: str = "code",
    scope: Optional[str] = "offline_access",
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
    storage: StorageBackend = Depends(get_storage),
):
    """Step 1 of 3-legged OAuth: Called by Gemini Enterprise in end-user browser."""
    if client_id != settings.GE_CLIENT_ID:
        logger.warning("Rejected authorize request with invalid client_id: %s", client_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid client_id",
        )

    # An unvalidated redirect_uri turns this endpoint into an open redirector and
    # leaks the authorization code to an attacker-controlled host. The error is
    # returned directly rather than redirected, because redirecting to an
    # unvalidated URI would defeat the check.
    if not _is_redirect_uri_allowed(redirect_uri):
        logger.warning(
            "Rejected authorize request with non-allowlisted redirect_uri: %s",
            redirect_uri,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid redirect_uri",
        )

    if response_type != "code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported response_type. Only 'code' is supported.",
        )

    session_id = str(uuid.uuid4())
    mv_verifier, mv_challenge = generate_pkce_pair()
    session_data = OAuthSessionData(
        session_id=session_id,
        google_redirect_uri=redirect_uri,
        google_state=state,
        google_code_challenge=code_challenge,
        google_code_challenge_method=code_challenge_method,
        upstream_code_verifier=mv_verifier,
    )

    await storage.save_session(
        session_id=session_id,
        data=session_data,
        ttl_seconds=settings.SESSION_TTL_SECONDS,
    )

    # Forward user's browser to upstream provider's official sign-in screen
    proxy_callback = f"{settings.PROXY_BASE_URL.rstrip('/')}/oauth/callback"
    params = {
        "response_type": "code",
        "client_id": settings.UPSTREAM_CLIENT_ID,
        "redirect_uri": proxy_callback,
        "state": session_id,
        "scope": settings.UPSTREAM_SCOPES,
        "code_challenge": mv_challenge,
        "code_challenge_method": "S256",
    }
    if settings.UPSTREAM_RESOURCE:
        params["resource"] = settings.UPSTREAM_RESOURCE
    if settings.UPSTREAM_AUDIENCE:
        params["audience"] = settings.UPSTREAM_AUDIENCE

    upstream_auth_redirect = f"{settings.UPSTREAM_AUTH_URL}?{urllib.parse.urlencode(params)}"
    logger.info("Redirecting user to %s auth with session %s", settings.UPSTREAM_SERVICE_NAME, session_id)
    return RedirectResponse(url=upstream_auth_redirect, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    storage: StorageBackend = Depends(get_storage),
):
    """Step 2: Receives redirect from upstream provider after user logs in."""
    if not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing state parameter")

    session = await storage.get_session(state)
    if not session:
        logger.warning("Callback state %s not found or expired", state)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired session state. Please retry sign-in from Gemini Enterprise.",
        )

    # Delete session state once retrieved to prevent reuse
    await storage.delete_session(state)

    # If upstream returned an error (e.g. user canceled consent)
    if error:
        logger.warning("%s returned auth error: %s (%s)", settings.UPSTREAM_SERVICE_NAME, error, error_description)
        err_params = {
            "error": error,
            "error_description": error_description or f"Consent denied or error at {settings.UPSTREAM_SERVICE_NAME}",
            "state": session.google_state,
        }
        redirect_url = f"{session.google_redirect_uri}?{urllib.parse.urlencode(err_params)}"
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing authorization code")

    # Exchange code with upstream token endpoint (including PKCE code_verifier)
    proxy_callback = f"{settings.PROXY_BASE_URL.rstrip('/')}/oauth/callback"
    try:
        upstream_tokens = await exchange_code_for_tokens(
            code=code,
            redirect_uri=proxy_callback,
            code_verifier=session.upstream_code_verifier,
        )
    except UpstreamAuthError as e:
        logger.error("Failed to exchange code with %s: %s", settings.UPSTREAM_SERVICE_NAME, e.detail)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Token exchange failed: {e.detail}")

    upstream_access_token = upstream_tokens.get("access_token")
    if not upstream_access_token:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{settings.UPSTREAM_SERVICE_NAME} did not return an access_token")

    upstream_refresh_token = upstream_tokens.get("refresh_token")
    expires_in = upstream_tokens.get("expires_in")
    upstream_expires_at = (time.time() + float(expires_in)) if expires_in else None

    # Generate internal proxy auth code for Gemini Enterprise
    proxy_auth_code = str(uuid.uuid4())
    auth_code_data = AuthCodeData(
        code=proxy_auth_code,
        upstream_access_token=upstream_access_token,
        upstream_refresh_token=upstream_refresh_token,
        upstream_expires_at=upstream_expires_at,
        google_code_challenge=session.google_code_challenge,
        google_code_challenge_method=session.google_code_challenge_method,
    )

    await storage.save_auth_code(
        code=proxy_auth_code,
        data=auth_code_data,
        ttl_seconds=settings.AUTH_CODE_TTL_SECONDS,
    )

    # Redirect user browser back to Google Vertex AI Search / Gemini Enterprise redirect URI
    google_params = {
        "code": proxy_auth_code,
        "state": session.google_state,
    }
    google_redirect = f"{session.google_redirect_uri}?{urllib.parse.urlencode(google_params)}"
    logger.info("Successfully bound %s token. Redirecting back to Google with code %s", settings.UPSTREAM_SERVICE_NAME, proxy_auth_code)
    return RedirectResponse(url=google_redirect, status_code=status.HTTP_302_FOUND)


@router.post("/token")
async def oauth_token(
    request: Request,
    storage: StorageBackend = Depends(get_storage),
):
    """Step 3: Server-to-server token endpoint called by Discovery Engine / Gemini Enterprise backend."""
    try:
        form_data = await request.form()
        form_dict = dict(form_data)
    except Exception:
        form_dict = {}

    auth_error = _authenticate_client(request, form_dict)
    if auth_error is not None:
        return auth_error

    grant_type = form_dict.get("grant_type", "authorization_code")

    if grant_type == "authorization_code":
        code = form_dict.get("code")
        if not code:
            logger.warning(
                "Token request rejected: no 'code' in form body. Parameters present: %s",
                sorted(form_dict.keys()),
            )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_request", "error_description": "Missing code parameter"},
            )

        auth_code_data = await storage.get_auth_code(code)
        if not auth_code_data:
            logger.warning(
                "Token request rejected: authorization code %s not found in storage "
                "(already redeemed, expired, or never issued)",
                code,
            )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_grant", "error_description": "Authorization code expired or invalid"},
            )

        # Single-use code: delete immediately
        await storage.delete_auth_code(code)

        # PKCE verification if challenge was set
        code_verifier = form_dict.get("code_verifier")
        if auth_code_data.google_code_challenge:
            if not verify_code_challenge(
                code_verifier=code_verifier,
                code_challenge=auth_code_data.google_code_challenge,
                method=auth_code_data.google_code_challenge_method,
            ):
                # The verifier is a secret and is never logged, only its shape. The
                # challenge is not: it is sent in the clear as an /authorize query
                # parameter, so echoing it alongside the value recomputed from the
                # verifier is what actually distinguishes "client sent no verifier"
                # from "client sent a verifier for a different challenge".
                logger.warning(
                    "Token request rejected: PKCE verification failed for code %s. "
                    "method=%s code_verifier_present=%s code_verifier_len=%d "
                    "expected_challenge=%s computed_challenge=%s",
                    code,
                    auth_code_data.google_code_challenge_method,
                    code_verifier is not None,
                    len(code_verifier or ""),
                    auth_code_data.google_code_challenge,
                    _compute_s256_challenge(code_verifier),
                )
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"error": "invalid_grant", "error_description": "PKCE verification failed"},
                )

        # Mint proxy access token
        proxy_access_token = str(uuid.uuid4())
        proxy_refresh_token = str(uuid.uuid4())
        now = time.time()

        user_token_data = UserTokenData(
            proxy_access_token=proxy_access_token,
            upstream_access_token=auth_code_data.upstream_access_token,
            upstream_refresh_token=auth_code_data.upstream_refresh_token,
            upstream_expires_at=auth_code_data.upstream_expires_at,
            proxy_refresh_token=proxy_refresh_token,
            # Stamped explicitly so the `expires_in` below is enforced rather than
            # merely advertised. Without it a token stays usable for the full Secret
            # Manager TTL, 30 days, regardless of what this response claims.
            proxy_expires_at=now + settings.PROXY_ACCESS_TOKEN_EXPIRES_IN,
            proxy_refresh_expires_at=now + settings.PROXY_REFRESH_TOKEN_EXPIRES_IN,
        )

        await storage.save_user_token(
            proxy_access_token=proxy_access_token,
            data=user_token_data,
        )

        logger.info("Minted new proxy access token for Gemini Enterprise")
        return {
            "access_token": proxy_access_token,
            "token_type": "Bearer",
            "expires_in": settings.PROXY_ACCESS_TOKEN_EXPIRES_IN,
            "refresh_token": proxy_refresh_token,
        }

    elif grant_type == "refresh_token":
        refresh_token = form_dict.get("refresh_token")
        if not refresh_token:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_request", "error_description": "Missing refresh_token"},
            )

        # An unknown refresh token must be rejected. Falling back to the most recent
        # user here would mint a proxy access token bound to a different employee's
        # session.
        user_token = await storage.get_user_token_by_refresh_token(refresh_token)

        if not user_token:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_grant", "error_description": "Refresh token expired or invalid"},
            )

        # Refresh tokens have an absolute ceiling anchored to the original sign-in.
        # Renewing that ceiling on every refresh would let a session persist forever
        # without the user ever re-authenticating against the upstream provider.
        if user_token.is_proxy_refresh_expired(settings.PROXY_REFRESH_TOKEN_EXPIRES_IN):
            logger.info("Rejected refresh grant: refresh token past its absolute expiry")
            await _revoke_session(storage, user_token)
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_grant",
                    "error_description": "Refresh token expired. Please re-authenticate.",
                },
            )

        # Auto-refresh upstream token if expired or near expiration
        is_expired = (
            user_token.upstream_expires_at is not None
            and time.time() > (user_token.upstream_expires_at - 60)
        )
        if is_expired and user_token.upstream_refresh_token:
            try:
                refreshed = await refresh_access_token(user_token.upstream_refresh_token)
                user_token.upstream_access_token = refreshed["access_token"]
                if "refresh_token" in refreshed:
                    user_token.upstream_refresh_token = refreshed["refresh_token"]
                if "expires_in" in refreshed:
                    user_token.upstream_expires_at = time.time() + float(refreshed["expires_in"])
            except UpstreamAuthError as e:
                logger.warning("Failed to refresh %s token during OAuth refresh grant: %s", settings.UPSTREAM_SERVICE_NAME, e)

        # Mint a replacement and retire the token it supersedes. The old token is not
        # deleted outright: requests issued moments before the refresh are still in
        # flight and would fail. It instead gets a short grace window, after which the
        # expiry check at /mcp rejects it.
        superseded_access_token = user_token.proxy_access_token
        new_proxy_access_token = str(uuid.uuid4())
        now = time.time()
        user_token.proxy_access_token = new_proxy_access_token
        user_token.proxy_refresh_token = refresh_token
        user_token.proxy_expires_at = now + settings.PROXY_ACCESS_TOKEN_EXPIRES_IN
        user_token.updated_at = now

        await storage.save_user_token(
            proxy_access_token=new_proxy_access_token,
            data=user_token,
        )

        if superseded_access_token and superseded_access_token != new_proxy_access_token:
            try:
                await storage.expire_user_token(
                    superseded_access_token,
                    now + settings.PROXY_TOKEN_REVOCATION_GRACE_SECONDS,
                )
            except Exception as e:
                # The replacement token is already issued and persisted. Failing the
                # grant here would strand the caller; the stale token still expires on
                # its own schedule, so log and continue.
                logger.warning("Could not retire superseded proxy access token: %s", e)

        logger.info("Refreshed proxy access token for Gemini Enterprise via refresh_token grant")
        return {
            "access_token": new_proxy_access_token,
            "token_type": "Bearer",
            "expires_in": settings.PROXY_ACCESS_TOKEN_EXPIRES_IN,
            "refresh_token": refresh_token,
        }


    else:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "unsupported_grant_type", "error_description": f"Unsupported grant_type: {grant_type}"},
        )


@router.post("/revoke")
async def oauth_revoke(
    request: Request,
    storage: StorageBackend = Depends(get_storage),
):
    """RFC 7009 token revocation.

    Accepts either a proxy access token or a proxy refresh token and tears down the
    whole session behind it. This is the operator's kill switch for a credential known
    to have leaked; before it existed the only recourse was deleting Secret Manager
    secrets by hand.
    """
    try:
        form_dict = dict(await request.form())
    except Exception:
        form_dict = {}

    auth_error = _authenticate_client(request, form_dict)
    if auth_error is not None:
        return auth_error

    token = form_dict.get("token")
    if not token:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_request", "error_description": "Missing token parameter"},
        )

    token_type_hint = form_dict.get("token_type_hint")

    # The hint only reorders the lookups; RFC 7009 requires both to be tried because
    # clients get the hint wrong.
    lookups = [storage.get_user_token, storage.get_user_token_by_refresh_token]
    if token_type_hint == "refresh_token":
        lookups.reverse()

    user_token = None
    for lookup in lookups:
        user_token = await lookup(token)
        if user_token:
            break

    if user_token:
        await _revoke_session(storage, user_token)
        logger.info("Revoked proxy session (token prefix=%s)", token[:8])
    else:
        logger.info("Revocation requested for unknown token (prefix=%s)", token[:8])

    # RFC 7009 s2.2: respond 200 whether or not the token existed, so this endpoint
    # cannot be used to probe which tokens are live.
    return Response(status_code=status.HTTP_200_OK)
