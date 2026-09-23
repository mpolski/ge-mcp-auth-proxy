"""Tests for vendor-agnostic OAuth proxying, storage schema neutrality, and DCR."""

import json
from unittest.mock import patch, AsyncMock
import pytest
from httpx import Response, Request
from pydantic import ValidationError

from app.config import settings
from app.storage.base import UserTokenData, AuthCodeData, OAuthSessionData
from app.oauth.client import exchange_code_for_tokens
from app.oauth.dcr import async_register_client, DCRError


def test_stored_token_schema_is_vendor_neutral():
    """The persisted token schema must not name any specific SaaS vendor.

    These fields are serialised into Secret Manager payloads, so a vendor name
    here would outlive any config change and follow every customer who deploys
    the broker against a different provider.
    """
    token = UserTokenData(
        proxy_access_token="ge-tok-888",
        upstream_access_token="up-access-token",
        upstream_refresh_token="up-refresh-token",
        upstream_expires_at=1800000000.0,
    )
    dumped = token.model_dump()
    assert dumped["upstream_access_token"] == "up-access-token"
    assert dumped["upstream_refresh_token"] == "up-refresh-token"
    assert dumped["upstream_expires_at"] == 1800000000.0
    assert not [k for k in dumped if "metaview" in k]


def test_vendor_named_token_keys_are_not_accepted():
    """A payload using the old metaview_* keys must fail loudly, not silently.

    Pydantic drops unknown keys, so without the required upstream_access_token
    such a payload would otherwise validate into a record holding no upstream
    credential at all, and the failure would surface much later as a 401 from
    the provider.
    """
    legacy_json = json.dumps({
        "proxy_access_token": "ge-tok-999",
        "metaview_access_token": "legacy-access-token",
    })
    with pytest.raises(ValidationError):
        UserTokenData.model_validate_json(legacy_json)


def test_session_and_code_data_use_generic_field_names():
    """AuthCodeData and OAuthSessionData carry upstream_*, not vendor-named, keys."""
    code = AuthCodeData(
        code="code-456",
        upstream_access_token="up-token-456",
        upstream_refresh_token="up-refresh-456",
    )
    assert code.upstream_access_token == "up-token-456"
    assert not [k for k in code.model_dump() if "metaview" in k]

    session = OAuthSessionData(
        session_id="sess-123",
        google_redirect_uri="https://vertexaisearch.cloud.google.com/oauth-redirect",
        google_state="state-123",
        upstream_code_verifier="up-verifier-123",
    )
    assert session.upstream_code_verifier == "up-verifier-123"
    assert not [k for k in session.model_dump() if "metaview" in k]


@pytest.mark.asyncio
async def test_vendor_token_exchange_with_audience_and_resource():
    """Verify that exchange_code_for_tokens transmits dynamic resource and audience parameters."""
    custom_token_endpoint = "https://auth.custom-saas.com/oauth/token"
    settings.UPSTREAM_TOKEN_URL = custom_token_endpoint
    settings.UPSTREAM_CLIENT_ID = "custom-client-id"
    settings.UPSTREAM_CLIENT_SECRET = "custom-client-secret"
    settings.UPSTREAM_RESOURCE = "https://mcp.custom-saas.com"
    settings.UPSTREAM_AUDIENCE = "https://api.custom-saas.com"

    mock_resp = Response(
        200,
        json={
            "access_token": "upstream-token-xyz",
            "refresh_token": "upstream-refresh-xyz",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        request=Request("POST", custom_token_endpoint),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp

        tokens = await exchange_code_for_tokens(
            code="auth-code-123",
            code_verifier="verifier-abc",
            redirect_uri="https://proxy.example.com/oauth/callback",
        )

        assert tokens["access_token"] == "upstream-token-xyz"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        sent_data = call_kwargs.get("data", {})
        assert sent_data.get("resource") == "https://mcp.custom-saas.com"
        assert sent_data.get("audience") == "https://api.custom-saas.com"
        assert sent_data.get("client_id") == "custom-client-id"


@pytest.mark.asyncio
async def test_dcr_registration_success():
    """Verify RFC 7591 dynamic client registration client."""
    dcr_endpoint = "https://auth.provider.com/oauth2/register"
    mock_resp = Response(
        201,
        json={
            "client_id": "registered-client-999",
            "client_secret": "registered-secret-888",
            "redirect_uris": ["https://proxy.example.com/oauth/callback"],
        },
        request=Request("POST", dcr_endpoint),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp

        result = await async_register_client(
            registration_url=dcr_endpoint,
            redirect_uris=["https://proxy.example.com/oauth/callback"],
            client_name="Test Proxy Client",
        )

        assert result["client_id"] == "registered-client-999"
        assert result["client_secret"] == "registered-secret-888"


@pytest.mark.asyncio
async def test_dcr_registration_failure():
    """Verify RFC 7591 error handling on registration rejection."""
    dcr_endpoint = "https://auth.provider.com/oauth2/register"
    mock_resp = Response(
        400,
        json={"error": "invalid_redirect_uri", "error_description": "URI rejected by policy"},
        request=Request("POST", dcr_endpoint),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp

        with pytest.raises(DCRError) as excinfo:
            await async_register_client(
                registration_url=dcr_endpoint,
                redirect_uris=["https://proxy.example.com/oauth/callback"],
            )

        assert "invalid_redirect_uri" in str(excinfo.value)
