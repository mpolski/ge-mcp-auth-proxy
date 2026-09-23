"""Tests for MCP proxy tool invocation, token swap, auto-refresh, and user isolation."""

from unittest.mock import patch, AsyncMock
import pytest
from httpx import AsyncClient, Response

from app.storage.base import UserTokenData


@pytest.mark.asyncio
async def test_mcp_missing_or_invalid_bearer(async_client: AsyncClient):
    # Missing authorization header
    resp = await async_client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list"})
    assert resp.status_code == 401

    # Non-existent token
    resp = await async_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list"},
        headers={"Authorization": "Bearer non-existent-token"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mcp_token_swap_and_proxy(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment
    proxy_token_a = "proxy-tok-user-a"
    user_a_data = UserTokenData(
        proxy_access_token=proxy_token_a,
        upstream_access_token="up-secret-token-a",
        upstream_refresh_token="up-refresh-a",
    )
    await store.save_user_token(proxy_token_a, user_a_data)

    mcp_request_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search_interviews", "arguments": {"query": "Alice"}},
    }

    mcp_upstream_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": "Interview with Alice: Passed"}]
        },
    }

    captured_headers = {}

    async def mock_forward(body, headers):
        nonlocal captured_headers
        captured_headers = headers
        return Response(200, json=mcp_upstream_response)

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        resp = await async_client.post(
            "/mcp",
            json=mcp_request_payload,
            headers={"Authorization": f"Bearer {proxy_token_a}"},
        )

    assert resp.status_code == 200
    assert resp.json() == mcp_upstream_response

    # Verify per-user token translation in upstream request
    assert captured_headers.get("Authorization") == "Bearer up-secret-token-a"


@pytest.mark.asyncio
async def test_mcp_multi_user_isolation(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment

    # User A
    tok_a = "proxy-token-user-a"
    await store.save_user_token(
        tok_a,
        UserTokenData(proxy_access_token=tok_a, upstream_access_token="up-token-user-a"),
    )

    # User B
    tok_b = "proxy-token-user-b"
    await store.save_user_token(
        tok_b,
        UserTokenData(proxy_access_token=tok_b, upstream_access_token="up-token-user-b"),
    )

    call_history = []

    async def mock_forward(body, headers):
        call_history.append(headers.get("Authorization"))
        return Response(200, json={"jsonrpc": "2.0", "result": "ok"})

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        # Call as User A
        await async_client.post(
            "/mcp",
            json={"method": "tools/call"},
            headers={"Authorization": f"Bearer {tok_a}"},
        )
        # Call as User B
        await async_client.post(
            "/mcp",
            json={"method": "tools/call"},
            headers={"Authorization": f"Bearer {tok_b}"},
        )

    assert call_history == [
        "Bearer up-token-user-a",
        "Bearer up-token-user-b",
    ]


@pytest.mark.asyncio
async def test_mcp_auto_refresh_on_401(async_client: AsyncClient, configure_test_environment):
    store = configure_test_environment
    proxy_token = "proxy-tok-stale"
    user_data = UserTokenData(
        proxy_access_token=proxy_token,
        upstream_access_token="up-expired-token",
        upstream_refresh_token="up-valid-refresh-token",
    )
    await store.save_user_token(proxy_token, user_data)

    mcp_call_count = 0

    async def mock_forward(body, headers):
        nonlocal mcp_call_count
        mcp_call_count += 1
        auth = headers.get("Authorization")

        # First call with expired token returns 401
        if auth == "Bearer up-expired-token":
            return Response(401, text="Unauthorized token expired")
        # Second call with refreshed token returns 200
        elif auth == "Bearer up-fresh-access-token":
            return Response(200, json={"jsonrpc": "2.0", "result": "refreshed_success"})
        return Response(400)

    mock_refresh_payload = {
        "access_token": "up-fresh-access-token",
        "refresh_token": "up-new-refresh-token",
        "expires_in": 3600,
    }

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward), \
         patch("app.mcp.proxy.refresh_access_token", new_callable=AsyncMock) as mock_refresh:

        mock_refresh.return_value = mock_refresh_payload

        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list"},
            headers={"Authorization": f"Bearer {proxy_token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["result"] == "refreshed_success"
    assert mcp_call_count == 2
    mock_refresh.assert_called_once_with("up-valid-refresh-token")

    # Verify store was updated with the fresh token
    updated_user_tok = await store.get_user_token(proxy_token)
    assert updated_user_tok.upstream_access_token == "up-fresh-access-token"
    assert updated_user_tok.upstream_refresh_token == "up-new-refresh-token"


@pytest.mark.asyncio
async def test_mcp_unknown_jwt_is_rejected_not_mapped_to_another_user(
    async_client: AsyncClient, configure_test_environment
):
    """An unrecognised token must never resolve to somebody else's upstream session.

    Regression test. The proxy previously fell back to `get_latest_user_token()` for
    any token it could not resolve, so an arbitrary bearer string was served using the
    most recent user's credentials.
    """
    store = configure_test_environment
    proxy_token = "proxy-tok-governed-user"
    user_data = UserTokenData(
        proxy_access_token=proxy_token,
        upstream_access_token="up-governed-token",
        proxy_refresh_token="proxy-ref-governed",
    )
    await store.save_user_token(proxy_token, user_data)

    forwarded = []

    async def mock_forward(body, headers):
        forwarded.append(headers.get("Authorization"))
        return Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    simulated_google_oidc_jwt = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjEyMyJ9.eyJpc3MiOiJodHRwczovL2FjY291bnRzLmdvb2dsZS5jb20ifQ.sig"
    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {simulated_google_oidc_jwt}"},
        )

    assert resp.status_code == 401
    # Nothing may reach the upstream provider using another user's credentials.
    assert forwarded == []


@pytest.mark.asyncio
async def test_mcp_garbage_token_does_not_borrow_active_session(
    async_client: AsyncClient, configure_test_environment
):
    """Even with active sessions present, an unknown opaque token gets 401."""
    store = configure_test_environment
    await store.save_user_token(
        "real-user-token",
        UserTokenData(
            proxy_access_token="real-user-token",
            upstream_access_token="up-real-user-token",
        ),
    )

    forwarded = []

    async def mock_forward(body, headers):
        forwarded.append(headers.get("Authorization"))
        return Response(200, json={"jsonrpc": "2.0", "result": "ok"})

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list"},
            headers={"Authorization": "Bearer totally-made-up-token"},
        )

    assert resp.status_code == 401
    assert forwarded == []


@pytest.mark.asyncio
async def test_oauth_refresh_grant_rejects_unknown_refresh_token(
    async_client: AsyncClient, configure_test_environment
):
    """An unknown refresh token must not mint a token bound to another user."""
    store = configure_test_environment
    await store.save_user_token(
        "some-user-token",
        UserTokenData(
            proxy_access_token="some-user-token",
            upstream_access_token="up-some-user",
            proxy_refresh_token="the-real-refresh-token",
        ),
    )

    resp = await async_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "not-a-real-refresh-token",
            "client_id": "test-ge-client",
            "client_secret": "test-ge-secret",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_oauth_refresh_token_grant(async_client: AsyncClient, configure_test_environment):
    """Verify that POST /oauth/token supports grant_type=refresh_token."""
    store = configure_test_environment
    proxy_token = "proxy-tok-initial"
    refresh_token = "proxy-refresh-token-123"
    user_data = UserTokenData(
        proxy_access_token=proxy_token,
        upstream_access_token="up-token-abc",
        proxy_refresh_token=refresh_token,
    )
    await store.save_user_token(proxy_token, user_data)

    resp = await async_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "test-ge-client",
            "client_secret": "test-ge-secret",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["refresh_token"] == refresh_token
    assert data["token_type"] == "Bearer"

