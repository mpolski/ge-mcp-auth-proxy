"""Tests for proxy access token expiry, refresh rotation, and revocation.

These cover the gap between what the token endpoint advertised and what it enforced.
`POST /oauth/token` has always returned `expires_in: 3600`, but nothing ever checked
that value: validation at /mcp only asked whether a record existed, so a token stayed
usable until its Secret Manager TTL removed it up to 30 days later. The refresh grant
made that worse by minting a replacement and leaving the old token valid, so a user
active for a month accumulated hundreds of simultaneously working credentials, none of
which could be withdrawn.
"""

import time
from unittest.mock import patch
import pytest
from httpx import AsyncClient, Response

from app.config import settings
from app.storage.base import AuthCodeData, UserTokenData


CLIENT_AUTH = {
    "client_id": "test-ge-client",
    "client_secret": "test-ge-secret",
}


async def _mint_session(async_client: AsyncClient, store, code: str = "lifecycle-code"):
    """Drive a real authorization_code grant and return its token response."""
    await store.save_auth_code(
        code,
        AuthCodeData(
            code=code,
            metaview_access_token="mv-acc-lifecycle",
            metaview_refresh_token="mv-ref-lifecycle",
        ),
    )
    resp = await async_client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": code, **CLIENT_AUTH},
    )
    assert resp.status_code == 200
    return resp.json()


# --------------------------------------------------------------------------------
# Expiry is stamped at mint
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorization_code_grant_stamps_expiry(async_client: AsyncClient, configure_test_environment):
    """The advertised `expires_in` must be recorded, not just returned."""
    store = configure_test_environment
    before = time.time()
    tokens = await _mint_session(async_client, store)
    after = time.time()

    record = await store.get_user_token(tokens["access_token"])
    assert record.proxy_expires_at is not None
    assert before + settings.PROXY_ACCESS_TOKEN_EXPIRES_IN <= record.proxy_expires_at
    assert record.proxy_expires_at <= after + settings.PROXY_ACCESS_TOKEN_EXPIRES_IN

    # The refresh token gets its own, longer ceiling.
    assert record.proxy_refresh_expires_at is not None
    assert record.proxy_refresh_expires_at > record.proxy_expires_at


# --------------------------------------------------------------------------------
# Expiry is enforced at /mcp
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_rejects_expired_proxy_access_token(async_client: AsyncClient, configure_test_environment):
    """An expired proxy token must not reach Metaview."""
    store = configure_test_environment
    token = "proxy-tok-expired"
    await store.save_user_token(
        token,
        UserTokenData(
            proxy_access_token=token,
            metaview_access_token="mv-should-not-be-used",
            proxy_expires_at=time.time() - 1,
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
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()
    assert forwarded == []


@pytest.mark.asyncio
async def test_mcp_accepts_unexpired_proxy_access_token(async_client: AsyncClient, configure_test_environment):
    """The expiry check must not reject a token that is still within its lifetime."""
    store = configure_test_environment
    token = "proxy-tok-fresh"
    await store.save_user_token(
        token,
        UserTokenData(
            proxy_access_token=token,
            metaview_access_token="mv-fresh",
            proxy_expires_at=time.time() + 300,
        ),
    )

    async def mock_forward(body, headers):
        return Response(200, json={"jsonrpc": "2.0", "result": "ok"})

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_legacy_record_without_expiry_falls_back_to_created_at(
    async_client: AsyncClient, configure_test_environment
):
    """Tokens issued before `proxy_expires_at` existed must still age out.

    Records already in Secret Manager when this shipped have no `proxy_expires_at`.
    Treating a missing value as "never expires" would leave every pre-existing token
    valid for the remainder of its 30-day TTL, which is the exact problem being fixed.
    """
    store = configure_test_environment
    token = "proxy-tok-legacy"
    await store.save_user_token(
        token,
        UserTokenData(
            proxy_access_token=token,
            metaview_access_token="mv-legacy",
            created_at=time.time() - (settings.PROXY_ACCESS_TOKEN_EXPIRES_IN + 60),
        ),
    )

    resp = await async_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401

    # A legacy record inside the fallback window is still honoured.
    recent = "proxy-tok-legacy-recent"
    await store.save_user_token(
        recent,
        UserTokenData(proxy_access_token=recent, metaview_access_token="mv-legacy-recent"),
    )

    async def mock_forward(body, headers):
        return Response(200, json={"jsonrpc": "2.0", "result": "ok"})

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list"},
            headers={"Authorization": f"Bearer {recent}"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_mcp_refresh_token_lookup_uses_refresh_lifetime(
    async_client: AsyncClient, configure_test_environment
):
    """A refresh token presented at /mcp is bound by the refresh lifetime, not the access one.

    Applying the one-hour access bound to this path would reject callers that are still
    entitled to renew, so the two lifetimes are checked separately.
    """
    store = configure_test_environment
    token = "proxy-tok-access"
    refresh = "proxy-ref-still-valid"
    await store.save_user_token(
        token,
        UserTokenData(
            proxy_access_token=token,
            metaview_access_token="mv-by-refresh",
            proxy_refresh_token=refresh,
            proxy_expires_at=time.time() - 1,
            proxy_refresh_expires_at=time.time() + 86400,
        ),
    )

    captured = []

    async def mock_forward(body, headers):
        captured.append(headers.get("Authorization"))
        return Response(200, json={"jsonrpc": "2.0", "result": "ok"})

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list"},
            headers={"Authorization": f"Bearer {refresh}"},
        )

    assert resp.status_code == 200
    assert captured == ["Bearer mv-by-refresh"]

    # Past its own ceiling, the refresh token stops working here too.
    await store.save_user_token(
        token,
        UserTokenData(
            proxy_access_token=token,
            metaview_access_token="mv-by-refresh",
            proxy_refresh_token=refresh,
            proxy_refresh_expires_at=time.time() - 1,
        ),
    )
    resp = await async_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list"},
        headers={"Authorization": f"Bearer {refresh}"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------------
# Refresh rotation retires the token it supersedes
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_retires_superseded_access_token(
    async_client: AsyncClient, configure_test_environment, monkeypatch
):
    """The token a refresh replaces must stop working, rather than stay valid.

    Regression test. The refresh grant used to mint a replacement and deliberately keep
    the previous token alive, so an hourly-refreshing session left roughly 170 working
    credentials behind over a month.
    """
    store = configure_test_environment
    monkeypatch.setattr(settings, "PROXY_TOKEN_REVOCATION_GRACE_SECONDS", 0)

    tokens = await _mint_session(async_client, store)
    old_token = tokens["access_token"]

    resp = await async_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            **CLIENT_AUTH,
        },
    )
    assert resp.status_code == 200
    new_token = resp.json()["access_token"]
    assert new_token != old_token

    async def mock_forward(body, headers):
        return Response(200, json={"jsonrpc": "2.0", "result": "ok"})

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        # The replacement works...
        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list"},
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert resp.status_code == 200

        # ...and the token it superseded does not.
        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list"},
            headers={"Authorization": f"Bearer {old_token}"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_grace_window_keeps_old_token_briefly(
    async_client: AsyncClient, configure_test_environment
):
    """The old token is retired after a grace window, not severed instantly.

    Requests issued moments before a refresh are still in flight; cutting them off at
    the instant of rotation would surface as sporadic 401s to the end user.
    """
    store = configure_test_environment
    tokens = await _mint_session(async_client, store)
    old_token = tokens["access_token"]
    original_expiry = (await store.get_user_token(old_token)).proxy_expires_at

    resp = await async_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            **CLIENT_AUTH,
        },
    )
    assert resp.status_code == 200

    retired = await store.get_user_token(old_token)
    assert retired.proxy_expires_at < original_expiry
    assert retired.proxy_expires_at <= time.time() + settings.PROXY_TOKEN_REVOCATION_GRACE_SECONDS

    # Still inside the grace window, so it continues to work for now.
    async def mock_forward(body, headers):
        return Response(200, json={"jsonrpc": "2.0", "result": "ok"})

    with patch("app.mcp.proxy.forward_mcp_request", side_effect=mock_forward):
        resp = await async_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list"},
            headers={"Authorization": f"Bearer {old_token}"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_refresh_succeeds_after_access_token_expires(
    async_client: AsyncClient, configure_test_environment
):
    """Access token expiry must not break the refresh grant - that is its whole purpose."""
    store = configure_test_environment
    token = "proxy-tok-aged-out"
    refresh = "proxy-ref-usable"
    await store.save_user_token(
        token,
        UserTokenData(
            proxy_access_token=token,
            metaview_access_token="mv-aged",
            proxy_refresh_token=refresh,
            proxy_expires_at=time.time() - 1,
            proxy_refresh_expires_at=time.time() + 86400,
        ),
    )

    resp = await async_client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh, **CLIENT_AUTH},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"] != token


@pytest.mark.asyncio
async def test_refresh_rejected_past_absolute_expiry(
    async_client: AsyncClient, configure_test_environment
):
    """A session cannot renew itself indefinitely.

    The refresh ceiling is anchored to the original sign-in, so after 30 days the user
    is sent back through Metaview rather than being silently extended forever.
    """
    store = configure_test_environment
    token = "proxy-tok-ancient"
    refresh = "proxy-ref-ancient"
    await store.save_user_token(
        token,
        UserTokenData(
            proxy_access_token=token,
            metaview_access_token="mv-ancient",
            proxy_refresh_token=refresh,
            proxy_refresh_expires_at=time.time() - 1,
        ),
    )

    resp = await async_client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh, **CLIENT_AUTH},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"

    # The dead session is torn down rather than left to linger.
    assert await store.get_user_token(token) is None


# --------------------------------------------------------------------------------
# Revocation
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_access_token_kills_session(async_client: AsyncClient, configure_test_environment):
    """Revoking an access token must immediately close off /mcp.

    `delete_user_token` existed in every backend from the start but nothing called it,
    so a credential known to have leaked could only be withdrawn by deleting Secret
    Manager secrets by hand.
    """
    store = configure_test_environment
    tokens = await _mint_session(async_client, store)

    resp = await async_client.post(
        "/oauth/revoke",
        data={"token": tokens["access_token"], **CLIENT_AUTH},
    )
    assert resp.status_code == 200

    assert await store.get_user_token(tokens["access_token"]) is None

    resp = await async_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoke_refresh_token_kills_whole_session(
    async_client: AsyncClient, configure_test_environment
):
    """Revoking by refresh token must also invalidate the access token it issued.

    A session is stored twice, keyed by access token and by refresh token. Removing
    only one leaves the other able to resolve the request, so the session would outlive
    its own revocation.
    """
    store = configure_test_environment
    tokens = await _mint_session(async_client, store)

    resp = await async_client.post(
        "/oauth/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "refresh_token",
            **CLIENT_AUTH,
        },
    )
    assert resp.status_code == 200

    assert await store.get_user_token(tokens["access_token"]) is None
    assert await store.get_user_token_by_refresh_token(tokens["refresh_token"]) is None

    # The refresh token can no longer mint anything either.
    resp = await async_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            **CLIENT_AUTH,
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_revoke_requires_client_authentication(
    async_client: AsyncClient, configure_test_environment
):
    """Revocation is a denial-of-service lever, so it must not be open to anyone."""
    store = configure_test_environment
    tokens = await _mint_session(async_client, store)

    resp = await async_client.post("/oauth/revoke", data={"token": tokens["access_token"]})
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"

    resp = await async_client.post(
        "/oauth/revoke",
        data={
            "token": tokens["access_token"],
            "client_id": "test-ge-client",
            "client_secret": "wrong-secret",
        },
    )
    assert resp.status_code == 401

    # The session survived both rejected attempts.
    assert await store.get_user_token(tokens["access_token"]) is not None


@pytest.mark.asyncio
async def test_revoke_unknown_token_returns_200(async_client: AsyncClient):
    """RFC 7009 s2.2. Returning 404 here would make the endpoint a token-validity oracle."""
    resp = await async_client.post(
        "/oauth/revoke",
        data={"token": "never-issued-this-token", **CLIENT_AUTH},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_revoke_requires_token_parameter(async_client: AsyncClient):
    resp = await async_client.post("/oauth/revoke", data=CLIENT_AUTH)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_revocation_endpoint_is_advertised(async_client: AsyncClient):
    """Clients discover revocation through RFC 8414 metadata."""
    resp = await async_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    data = resp.json()
    assert data["revocation_endpoint"].endswith("/oauth/revoke")
    assert "refresh_token" in data["grant_types_supported"]
