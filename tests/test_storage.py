"""Unit tests for storage backend."""

import asyncio
import pytest
from app.storage.base import OAuthSessionData, AuthCodeData, UserTokenData
from app.storage.memory import MemoryStorage


@pytest.mark.asyncio
async def test_session_lifecycle():
    store = MemoryStorage()
    sess = OAuthSessionData(
        session_id="sess-123",
        google_redirect_uri="https://vertexaisearch.cloud.google.com/oauth-redirect",
        google_state="state-abc",
    )
    await store.save_session("sess-123", sess, ttl_seconds=3600)

    retrieved = await store.get_session("sess-123")
    assert retrieved is not None
    assert retrieved.google_state == "state-abc"

    await store.delete_session("sess-123")
    assert await store.get_session("sess-123") is None


@pytest.mark.asyncio
async def test_session_expiry():
    store = MemoryStorage()
    sess = OAuthSessionData(
        session_id="sess-exp",
        google_redirect_uri="https://example.com",
        google_state="s",
    )
    # 0 second TTL expires immediately
    await store.save_session("sess-exp", sess, ttl_seconds=-1)
    assert await store.get_session("sess-exp") is None


@pytest.mark.asyncio
async def test_auth_code_lifecycle():
    store = MemoryStorage()
    code_data = AuthCodeData(
        code="code-456",
        upstream_access_token="up-acc-token",
        upstream_refresh_token="up-ref-token",
    )
    await store.save_auth_code("code-456", code_data, ttl_seconds=300)

    retrieved = await store.get_auth_code("code-456")
    assert retrieved is not None
    assert retrieved.upstream_access_token == "up-acc-token"

    await store.delete_auth_code("code-456")
    assert await store.get_auth_code("code-456") is None


@pytest.mark.asyncio
async def test_user_token_lifecycle_and_update():
    store = MemoryStorage()
    user_data = UserTokenData(
        proxy_access_token="proxy-tok-789",
        upstream_access_token="up-tok-initial",
        upstream_refresh_token="up-ref-1",
    )
    await store.save_user_token("proxy-tok-789", user_data)

    retrieved = await store.get_user_token("proxy-tok-789")
    assert retrieved is not None
    assert retrieved.upstream_access_token == "up-tok-initial"

    # Update token on refresh
    user_data.upstream_access_token = "up-tok-refreshed"
    await store.update_user_token("proxy-tok-789", user_data)

    updated = await store.get_user_token("proxy-tok-789")
    assert updated is not None
    assert updated.upstream_access_token == "up-tok-refreshed"
