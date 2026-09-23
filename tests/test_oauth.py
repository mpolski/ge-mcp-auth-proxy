"""Tests for OAuth 2.0 authorization, callback, and token endpoints."""

import base64
import hashlib
import urllib.parse
from unittest.mock import patch
import pytest
from httpx import AsyncClient, Response

from app.config import settings
from app.storage.base import OAuthSessionData, AuthCodeData


@pytest.mark.asyncio
async def test_health_check(async_client: AsyncClient):
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_oauth_metadata(async_client: AsyncClient):
    resp = await async_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    data = resp.json()
    assert "/oauth/authorize" in data["authorization_endpoint"]
    assert "/oauth/token" in data["token_endpoint"]


@pytest.mark.asyncio
async def test_oauth_authorize_success(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment
    params = {
        "client_id": settings.GE_CLIENT_ID,
        "redirect_uri": "https://vertexaisearch.cloud.google.com/oauth-redirect",
        "state": "google-state-12345",
        "response_type": "code",
        "code_challenge": "E9Melhoa2OwvFrGMTJguCH5rtx64EH-qc8nk53n7BqU",
        "code_challenge_method": "S256",
    }
    resp = await async_client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "https://auth.metaview.ai/oauth2/authorize" in location

    parsed = urllib.parse.urlparse(location)
    q = urllib.parse.parse_qs(parsed.query)
    assert q["client_id"] == [settings.METAVIEW_CLIENT_ID]
    assert q["redirect_uri"] == [f"{settings.PROXY_BASE_URL}/oauth/callback"]
    assert q["resource"] == [settings.METAVIEW_MCP_URL]
    session_id = q["state"][0]

    # Verify session was persisted in storage
    session = await store.get_session(session_id)
    assert session is not None
    assert session.google_redirect_uri == params["redirect_uri"]
    assert session.google_state == params["state"]
    assert session.google_code_challenge == params["code_challenge"]


@pytest.mark.asyncio
async def test_oauth_authorize_invalid_client_id(async_client: AsyncClient):
    params = {
        "client_id": "wrong-client-id",
        "redirect_uri": "https://vertexaisearch.cloud.google.com/oauth-redirect",
        "state": "google-state-12345",
    }
    resp = await async_client.get("/oauth/authorize", params=params)
    assert resp.status_code == 400
    assert "Invalid client_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_oauth_callback_success(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment
    session_id = "test-session-xyz"
    session_data = OAuthSessionData(
        session_id=session_id,
        google_redirect_uri="https://vertexaisearch.cloud.google.com/oauth-redirect",
        google_state="google-state-xyz",
        google_code_challenge="challenge-123",
        google_code_challenge_method="S256",
    )
    await store.save_session(session_id, session_data)

    mock_metaview_response = {
        "access_token": "mv-access-tok-111",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "mv-refresh-tok-222",
    }

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = Response(200, json=mock_metaview_response)

        resp = await async_client.get(
            "/oauth/callback",
            params={"code": "mv-auth-code-999", "state": session_id},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://vertexaisearch.cloud.google.com/oauth-redirect")

    parsed = urllib.parse.urlparse(location)
    q = urllib.parse.parse_qs(parsed.query)
    assert q["state"] == ["google-state-xyz"]
    proxy_auth_code = q["code"][0]

    # Verify session was deleted
    assert await store.get_session(session_id) is None

    # Verify auth code was saved with Metaview tokens
    saved_code = await store.get_auth_code(proxy_auth_code)
    assert saved_code is not None
    assert saved_code.metaview_access_token == "mv-access-tok-111"
    assert saved_code.metaview_refresh_token == "mv-refresh-tok-222"
    assert saved_code.google_code_challenge == "challenge-123"


@pytest.mark.asyncio
async def test_oauth_callback_user_denied(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment
    session_id = "test-session-denied"
    session_data = OAuthSessionData(
        session_id=session_id,
        google_redirect_uri="https://vertexaisearch.cloud.google.com/oauth-redirect",
        google_state="google-state-denied",
    )
    await store.save_session(session_id, session_data)

    resp = await async_client.get(
        "/oauth/callback",
        params={
            "error": "access_denied",
            "error_description": "The user canceled authentication",
            "state": session_id,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    parsed = urllib.parse.urlparse(location)
    q = urllib.parse.parse_qs(parsed.query)
    assert q["error"] == ["access_denied"]
    assert q["state"] == ["google-state-denied"]


@pytest.mark.asyncio
async def test_oauth_token_exchange_form_auth(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment
    code = "auth-code-valid"
    code_data = AuthCodeData(
        code=code,
        metaview_access_token="mv-acc-555",
        metaview_refresh_token="mv-ref-666",
    )
    await store.save_auth_code(code, code_data)

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.GE_CLIENT_ID,
        "client_secret": settings.GE_CLIENT_SECRET,
    }
    resp = await async_client.post("/oauth/token", data=data)
    assert resp.status_code == 200
    token_json = resp.json()
    assert "access_token" in token_json
    assert token_json["token_type"] == "Bearer"
    assert token_json["expires_in"] == settings.PROXY_ACCESS_TOKEN_EXPIRES_IN

    # Verify code is single-use and now deleted
    assert await store.get_auth_code(code) is None

    # Verify user token is stored and maps to Metaview token
    proxy_token = token_json["access_token"]
    user_tok = await store.get_user_token(proxy_token)
    assert user_tok is not None
    assert user_tok.metaview_access_token == "mv-acc-555"
    assert user_tok.metaview_refresh_token == "mv-ref-666"


@pytest.mark.asyncio
async def test_oauth_token_exchange_basic_auth(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment
    code = "auth-code-basic"
    code_data = AuthCodeData(
        code=code,
        metaview_access_token="mv-acc-777",
    )
    await store.save_auth_code(code, code_data)

    auth_str = f"{settings.GE_CLIENT_ID}:{settings.GE_CLIENT_SECRET}"
    basic_auth = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
    headers = {"Authorization": f"Basic {basic_auth}"}

    resp = await async_client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": code},
        headers=headers,
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_oauth_token_pkce_verification(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment
    verifier = "high_entropy_secret_code_verifier_12345678901234567890"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    code = "auth-code-pkce"
    code_data = AuthCodeData(
        code=code,
        metaview_access_token="mv-acc-pkce",
        google_code_challenge=challenge,
        google_code_challenge_method="S256",
    )
    await store.save_auth_code(code, code_data)

    # 1. Invalid verifier should fail
    resp = await async_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.GE_CLIENT_ID,
            "client_secret": settings.GE_CLIENT_SECRET,
            "code_verifier": "wrong-verifier",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"

    # Re-save code because failed attempt deletes it or try with valid verifier
    await store.save_auth_code(code, code_data)
    resp = await async_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.GE_CLIENT_ID,
            "client_secret": settings.GE_CLIENT_SECRET,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_oauth_token_unauthorized_client(async_client: AsyncClient):
    resp = await async_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "code",
            "client_id": "bad-client",
            "client_secret": "bad-secret",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


@pytest.mark.asyncio
async def test_test_console_not_mounted_by_default(async_client: AsyncClient):
    """The browser test console must be absent unless explicitly enabled.

    Regression test. These routes were previously always mounted. `/test-callback`
    rendered GE_CLIENT_SECRET into the returned HTML and `/test/active-token` returned
    a live proxy access token, both without authentication, so on a service deployed
    with --allow-unauthenticated they formed a complete anonymous path to a user's
    Metaview data.
    """
    assert settings.ENABLE_TEST_CONSOLE is False

    for path in ("/test", "/test-callback", "/test/active-token"):
        resp = await async_client.get(path)
        assert resp.status_code == 404, f"{path} should not be reachable by default"

    resp = await async_client.post("/test/exchange", data={"code": "x"})
    assert resp.status_code == 404

    # The client secret must never appear in any response body.
    resp = await async_client.get("/test-callback?code=mock-code&state=mock-state")
    assert settings.GE_CLIENT_SECRET not in resp.text

