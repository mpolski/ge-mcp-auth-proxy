"""Client for upstream OAuth 2.0 endpoints (Metaview, Greenhouse, Carta, etc.)."""

import logging
from typing import Dict, Any, Optional
import httpx
from app.config import settings

logger = logging.getLogger(__name__)


class UpstreamAuthError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail




async def exchange_code_for_tokens(
    code: str,
    redirect_uri: str,
    code_verifier: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchange authorization code received from upstream for user access & refresh tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.UPSTREAM_CLIENT_ID,
        "redirect_uri": redirect_uri,
    }
    if settings.UPSTREAM_RESOURCE:
        data["resource"] = settings.UPSTREAM_RESOURCE
    if settings.UPSTREAM_AUDIENCE:
        data["audience"] = settings.UPSTREAM_AUDIENCE
    if code_verifier:
        data["code_verifier"] = code_verifier
    if settings.UPSTREAM_CLIENT_SECRET:
        data["client_secret"] = settings.UPSTREAM_CLIENT_SECRET

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                settings.UPSTREAM_TOKEN_URL,
                data=data,
                headers={"Accept": "application/json"},
            )
        except Exception as e:
            logger.error("Network error connecting to upstream token endpoint (%s): %s", settings.UPSTREAM_SERVICE_NAME, e)
            raise UpstreamAuthError(502, f"Failed to reach {settings.UPSTREAM_SERVICE_NAME} token endpoint: {e}")

    if resp.status_code != 200:
        logger.error(
            "%s token exchange failed with status %d: %s",
            settings.UPSTREAM_SERVICE_NAME,
            resp.status_code,
            resp.text,
        )
        raise UpstreamAuthError(
            resp.status_code,
            f"{settings.UPSTREAM_SERVICE_NAME} token exchange failed: {resp.text}",
        )

    return resp.json()


async def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Exchange upstream refresh token for a fresh access token."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.UPSTREAM_CLIENT_ID,
    }
    if settings.UPSTREAM_RESOURCE:
        data["resource"] = settings.UPSTREAM_RESOURCE
    if settings.UPSTREAM_AUDIENCE:
        data["audience"] = settings.UPSTREAM_AUDIENCE
    if settings.UPSTREAM_CLIENT_SECRET:
        data["client_secret"] = settings.UPSTREAM_CLIENT_SECRET

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                settings.UPSTREAM_TOKEN_URL,
                data=data,
                headers={"Accept": "application/json"},
            )
        except Exception as e:
            logger.error("Network error refreshing %s token: %s", settings.UPSTREAM_SERVICE_NAME, e)
            raise UpstreamAuthError(502, f"Failed to refresh {settings.UPSTREAM_SERVICE_NAME} token: {e}")

    if resp.status_code != 200:
        logger.error(
            "%s token refresh failed with status %d: %s",
            settings.UPSTREAM_SERVICE_NAME,
            resp.status_code,
            resp.text,
        )
        raise UpstreamAuthError(
            resp.status_code,
            f"{settings.UPSTREAM_SERVICE_NAME} token refresh failed: {resp.text}",
        )

    return resp.json()
