"""Tests for the /oauth/authorize redirect_uri allowlist (RFC 6749 3.1.2.2).

An authorization server that accepts an arbitrary redirect_uri is an open
redirector and leaks the authorization code to whatever host the caller names.
These tests pin the exact-match behaviour and the common bypass shapes.
"""

import urllib.parse

import pytest
from httpx import AsyncClient

from app.config import settings

GE_REDIRECT = "https://vertexaisearch.cloud.google.com/oauth-redirect"
AUTH_MANAGER_REDIRECT = (
    "https://iamconnectorcredentials.googleapis.com/v1/projects/p/locations/l"
    "/connectors/c/oauthcallback"
)


def _params(redirect_uri: str) -> dict:
    return {
        "client_id": settings.GE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": "state-abc",
        "response_type": "code",
        "code_challenge": "E9Melhoa2OwvFrGMTJguCH5rtx64EH-qc8nk53n7BqU",
        "code_challenge_method": "S256",
    }


@pytest.mark.asyncio
async def test_allowlisted_redirect_uri_is_accepted(
    async_client: AsyncClient, configure_test_environment, monkeypatch
):
    monkeypatch.setattr(settings, "GE_ALLOWED_REDIRECT_URIS", GE_REDIRECT)
    resp = await async_client.get(
        "/oauth/authorize", params=_params(GE_REDIRECT), follow_redirects=False
    )
    assert resp.status_code == 302


@pytest.mark.asyncio
async def test_non_allowlisted_redirect_uri_is_rejected(
    async_client: AsyncClient, configure_test_environment, monkeypatch
):
    monkeypatch.setattr(settings, "GE_ALLOWED_REDIRECT_URIS", GE_REDIRECT)
    resp = await async_client.get(
        "/oauth/authorize",
        params=_params("https://evil.example/steal"),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    # Must NOT redirect to the rejected URI - that would defeat the check.
    assert "location" not in {k.lower() for k in resp.headers}


@pytest.mark.parametrize(
    "bypass",
    [
        # Suffix-appended host: passes a naive startswith() check.
        "https://vertexaisearch.cloud.google.com.evil.example/oauth-redirect",
        # Path suffix: passes a naive startswith() check.
        GE_REDIRECT + ".evil.example",
        # Open-redirect style path traversal.
        "https://vertexaisearch.cloud.google.com/oauth-redirect/../../evil",
        # Scheme downgrade.
        "http://vertexaisearch.cloud.google.com/oauth-redirect",
        # Userinfo trick: real host is evil.example.
        "https://vertexaisearch.cloud.google.com@evil.example/oauth-redirect",
    ],
)
@pytest.mark.asyncio
async def test_common_bypass_shapes_are_rejected(
    async_client: AsyncClient, configure_test_environment, monkeypatch, bypass
):
    monkeypatch.setattr(settings, "GE_ALLOWED_REDIRECT_URIS", GE_REDIRECT)
    resp = await async_client.get(
        "/oauth/authorize", params=_params(bypass), follow_redirects=False
    )
    assert resp.status_code == 400, f"bypass was accepted: {bypass}"


@pytest.mark.asyncio
async def test_rejected_request_persists_no_session(
    async_client: AsyncClient, configure_test_environment, monkeypatch
):
    """A rejected authorize must not leave state behind in the token store."""
    store = configure_test_environment
    monkeypatch.setattr(settings, "GE_ALLOWED_REDIRECT_URIS", GE_REDIRECT)

    resp = await async_client.get(
        "/oauth/authorize",
        params=_params("https://evil.example/steal"),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert getattr(store, "sessions", {}) == {}


@pytest.mark.asyncio
async def test_multiple_allowlisted_uris(
    async_client: AsyncClient, configure_test_environment, monkeypatch
):
    """Agent Identity auth manager callback can be added alongside the GE redirect."""
    monkeypatch.setattr(
        settings,
        "GE_ALLOWED_REDIRECT_URIS",
        f"{GE_REDIRECT}, {AUTH_MANAGER_REDIRECT}",
    )
    for uri in (GE_REDIRECT, AUTH_MANAGER_REDIRECT):
        resp = await async_client.get(
            "/oauth/authorize", params=_params(uri), follow_redirects=False
        )
        assert resp.status_code == 302, f"expected {uri} to be allowed"


@pytest.mark.asyncio
async def test_wildcard_disables_enforcement(
    async_client: AsyncClient, configure_test_environment, monkeypatch
):
    """'*' is the documented escape hatch for local development."""
    monkeypatch.setattr(settings, "GE_ALLOWED_REDIRECT_URIS", "*")
    resp = await async_client.get(
        "/oauth/authorize",
        params=_params("https://anything.example/cb"),
        follow_redirects=False,
    )
    assert resp.status_code == 302


@pytest.mark.asyncio
async def test_default_setting_allows_gemini_enterprise_redirect():
    """The out-of-the-box default must permit the real GE callback."""
    default = type(settings)().GE_ALLOWED_REDIRECT_URIS
    assert GE_REDIRECT in [u.strip() for u in default.split(",")]


@pytest.mark.asyncio
async def test_allowlisted_uri_is_forwarded_unmodified(
    async_client: AsyncClient, configure_test_environment, monkeypatch
):
    """The accepted redirect_uri must survive into the upstream session state."""
    store = configure_test_environment
    monkeypatch.setattr(settings, "GE_ALLOWED_REDIRECT_URIS", GE_REDIRECT)

    resp = await async_client.get(
        "/oauth/authorize", params=_params(GE_REDIRECT), follow_redirects=False
    )
    assert resp.status_code == 302

    q = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)
    session = await store.get_session(q["state"][0])
    assert session is not None
    assert session.google_redirect_uri == GE_REDIRECT
