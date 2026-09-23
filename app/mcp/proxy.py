"""MCP (Model Context Protocol) Runtime Proxy with Per-User Token Isolation and Auto-Refresh."""

import json
import logging
import os
import time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
import httpx

from app.config import settings
from app.oauth.client import refresh_access_token, UpstreamAuthError
from app.storage import get_storage, StorageBackend, UserTokenData
from app.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)
router = APIRouter(tags=["MCP Proxy"])

# Headers to filter out before forwarding to the upstream MCP server
EXCLUDED_REQUEST_HEADERS = {
    "host",
    "authorization",
    "content-length",
    "connection",
    "transfer-encoding",
    "origin",
    "referer",
    "user-agent",
    "cookie",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
}

# Headers to exclude from response back to client
EXCLUDED_RESPONSE_HEADERS = {
    "content-length",
    "connection",
    "transfer-encoding",
    "content-encoding",
}


async def forward_mcp_request(body: bytes, headers: dict) -> httpx.Response:
    """Forward MCP JSON-RPC payload to upstream MCP server."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await client.post(
            settings.UPSTREAM_MCP_URL,
            content=body,
            headers=headers,
        )


def _mcp_server_urn() -> str:
    """Build the Agent Registry MCP server URN used to correlate spans.

    Derived from configuration rather than hardcoded so the service produces
    correctly attributed traces in whichever project and region it is deployed to.
    """
    explicit = os.environ.get("MCP_SERVER_URN")
    if explicit:
        return explicit

    service_name = os.environ.get("K_SERVICE", f"ge-{settings.UPSTREAM_SERVICE_NAME}-proxy")
    project_number = settings.GCP_PROJECT_NUMBER or settings.GCP_PROJECT_ID or "unknown"
    return (
        f"urn:mcp:projects-{project_number}:projects:{project_number}"
        f":locations:{settings.GCP_REGION}:run:services:{service_name}"
    )


async def _resolve_and_refresh_token(
    proxy_token: str,
    user_token: UserTokenData,
    storage: StorageBackend,
    force_refresh: bool = False,
) -> str:
    """Resolve upstream access token, automatically refreshing if expired or forced."""
    is_expired = (
        user_token.upstream_expires_at is not None
        and time.time() > (user_token.upstream_expires_at - 60)
    )

    if (is_expired or force_refresh) and user_token.upstream_refresh_token:
        with tracer.start_as_current_span("mcp.oauth.refresh_token") as span:
            span.set_attribute("mcp.token.force_refresh", force_refresh)
            span.set_attribute("mcp.token.is_expired", is_expired)
            logger.info("%s access token expired or 401 received. Auto-refreshing using refresh token...", settings.UPSTREAM_SERVICE_NAME)
            try:
                refreshed = await refresh_access_token(user_token.upstream_refresh_token)
                user_token.upstream_access_token = refreshed["access_token"]
                if "refresh_token" in refreshed:
                    user_token.upstream_refresh_token = refreshed["refresh_token"]
                if "expires_in" in refreshed:
                    user_token.upstream_expires_at = time.time() + float(refreshed["expires_in"])

                await storage.update_user_token(proxy_token, user_token)
                span.set_attribute("mcp.token.refresh_success", True)
                logger.info("Successfully refreshed and persisted %s token for proxy session", settings.UPSTREAM_SERVICE_NAME)
            except UpstreamAuthError as e:
                span.set_attribute("mcp.token.refresh_success", False)
                span.record_exception(e)
                logger.error("Failed to auto-refresh %s token: %s", settings.UPSTREAM_SERVICE_NAME, e)
                if force_refresh:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail=f"{settings.UPSTREAM_SERVICE_NAME.capitalize()} session expired and refresh failed. Please re-authenticate.",
                    )

    return user_token.upstream_access_token


def _prepare_headers(request: Request, upstream_token: str) -> dict:
    """Prepare clean forward headers with per-user upstream Bearer token."""
    headers = {}
    for key, val in request.headers.items():
        k = key.lower()
        if k in EXCLUDED_REQUEST_HEADERS or k.startswith("sec-"):
            continue
        headers[key] = val
    headers["Authorization"] = f"Bearer {upstream_token}"
    headers["Accept"] = "application/json, text/event-stream"
    return headers


async def _resolve_request_user_token(
    request: Request,
    storage: StorageBackend,
) -> tuple[str, UserTokenData]:
    """Extract the Bearer token from the request and resolve it to UserTokenData."""
    candidate_tokens = []
    for hdr in ("X-Forwarded-Authorization", "Authorization"):
        val = request.headers.get(hdr, "").strip()
        if val.startswith("Bearer "):
            tok = val.replace("Bearer ", "", 1).strip()
            if tok and tok not in candidate_tokens:
                candidate_tokens.append(tok)

    if not candidate_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Bearer authorization token",
        )

    primary_token = candidate_tokens[0]

    with tracer.start_as_current_span("mcp.storage.lookup_user_token") as lookup_span:
        lookup_span.set_attribute("mcp.auth.token_prefix", primary_token[:12])
        lookup_span.set_attribute("mcp.auth.is_jwt", primary_token.startswith("eyJ"))

        # Tracks whether a token resolved to a real session but was past its expiry,
        # so the 401 can say "expired" instead of "invalid" and prompt a refresh.
        saw_expired = False

        # 1. Try direct access token lookup
        for tok in candidate_tokens:
            user_token = await storage.get_user_token(tok)
            if not user_token:
                continue
            # Expiry is enforced here rather than trusted from the token response.
            # Until this check existed, `expires_in` was advertised to Gemini
            # Enterprise but never applied: a token remained usable until its Secret
            # Manager TTL removed it, up to 30 days after it was issued.
            if user_token.is_proxy_token_expired(settings.PROXY_ACCESS_TOKEN_EXPIRES_IN):
                saw_expired = True
                continue
            lookup_span.set_attribute("mcp.token.found", True)
            lookup_span.set_attribute("mcp.token.lookup_method", "access_token")
            return tok, user_token

        # 2. Try refresh token lookup
        for tok in candidate_tokens:
            user_token = await storage.get_user_token_by_refresh_token(tok)
            if not user_token:
                continue
            # Checked against the refresh lifetime, not the access lifetime. A refresh
            # token is meant to outlive the access token it renews, so applying the
            # shorter bound here would reject a caller that is still entitled to one.
            if user_token.is_proxy_refresh_expired(settings.PROXY_REFRESH_TOKEN_EXPIRES_IN):
                saw_expired = True
                continue
            lookup_span.set_attribute("mcp.token.found", True)
            lookup_span.set_attribute("mcp.token.lookup_method", "refresh_token")
            logger.info("Resolved MCP request via refresh_token lookup (prefix=%s)", tok[:8])
            return user_token.proxy_access_token, user_token

        if saw_expired:
            lookup_span.set_attribute("mcp.token.found", True)
            lookup_span.set_attribute("mcp.token.expired", True)
            logger.info(
                "MCP request rejected: token (prefix=%s) is past its expiry",
                primary_token[:12],
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Proxy access token expired. Refresh the token or re-authenticate.",
            )

        # No match. We deliberately do NOT fall back to "the most recently active user":
        # doing so would hand the caller another employee's upstream credentials and
        # collapse the per-user isolation this proxy exists to enforce.
        lookup_span.set_attribute("mcp.token.found", False)
        logger.warning(
            "MCP request rejected: token (prefix=%s, is_jwt=%s) not found in storage",
            primary_token[:12],
            primary_token.startswith("eyJ"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Session expired or invalid. Please re-authenticate with {settings.UPSTREAM_SERVICE_NAME}.",
        )


@router.post("/mcp")
@router.post("/mcp/")
async def mcp_post_proxy(
    request: Request,
    storage: StorageBackend = Depends(get_storage),
):
    """Runtime MCP Tool Invocation proxy.
    Swaps the Gemini Enterprise proxy Bearer token for the caller's personal upstream token.
    """
    proxy_token, user_token = await _resolve_request_user_token(request, storage)


    # 1. Resolve token (auto-refresh if near expiration)
    upstream_token = await _resolve_and_refresh_token(
        proxy_token=proxy_token,
        user_token=user_token,
        storage=storage,
        force_refresh=False,
    )

    body = await request.body()
    forward_headers = _prepare_headers(request, upstream_token)

    # Extract MCP JSON-RPC metadata for OpenTelemetry span enrichment
    rpc_method = "unknown"
    rpc_id = None
    tool_name = None
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            rpc_method = payload.get("method", "unknown")
            rpc_id = payload.get("id")
            params = payload.get("params")
            if isinstance(params, dict) and "name" in params:
                tool_name = params.get("name")
    except Exception:
        pass

    mcp_server_urn = _mcp_server_urn()

    with tracer.start_as_current_span("mcp.upstream.forward") as forward_span:
        forward_span.set_attribute("mcp.jsonrpc.method", rpc_method)
        forward_span.set_attribute("mcp.method.name", rpc_method)
        forward_span.set_attribute("jsonrpc.protocol.version", "2.0")
        if rpc_id is not None:
            forward_span.set_attribute("jsonrpc.request.id", str(rpc_id))
        forward_span.set_attribute("gen_ai.system", settings.UPSTREAM_SERVICE_NAME)
        forward_span.set_attribute("gcp.mcp.server.id", mcp_server_urn)
        if tool_name:
            forward_span.set_attribute("mcp.tool.name", str(tool_name))
            forward_span.set_attribute("gen_ai.operation.name", "execute_tool")
            forward_span.set_attribute("gen_ai.tool.name", str(tool_name))

        # Also create the official Google Cloud MCP span `tools/call <NAME>` per OpenTelemetry MCP Semantic Conventions
        tool_span_ctx = (
            tracer.start_as_current_span(f"tools/call {tool_name}")
            if tool_name
            else None
        )
        tool_span = tool_span_ctx.__enter__() if tool_span_ctx else None
        if tool_span:
            tool_span.set_attribute("mcp.method.name", rpc_method)
            tool_span.set_attribute("jsonrpc.protocol.version", "2.0")
            if rpc_id is not None:
                tool_span.set_attribute("jsonrpc.request.id", str(rpc_id))
            tool_span.set_attribute("gen_ai.system", settings.UPSTREAM_SERVICE_NAME)
            tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
            tool_span.set_attribute("gen_ai.tool.name", str(tool_name))
            tool_span.set_attribute("gcp.mcp.server.id", mcp_server_urn)


        try:
            # 2. Forward request to upstream MCP
            try:
                resp = await forward_mcp_request(body, forward_headers)
                forward_span.set_attribute("mcp.upstream.status_code", resp.status_code)
            except Exception as e:
                forward_span.record_exception(e)
                if tool_span:
                    tool_span.record_exception(e)
                logger.error("Error communicating with %s MCP endpoint: %s", settings.UPSTREAM_SERVICE_NAME, e)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Upstream {settings.UPSTREAM_SERVICE_NAME} MCP server unreachable: {e}",
                )

            # 3. If upstream returned 401, attempt one retry after refreshing token
            if resp.status_code == 401 and user_token.upstream_refresh_token:
                forward_span.set_attribute("mcp.token.retry_on_401", True)
                logger.warning("%s MCP returned 401 Unauthorized. Retrying after token refresh...", settings.UPSTREAM_SERVICE_NAME)
                upstream_token = await _resolve_and_refresh_token(
                    proxy_token=proxy_token,
                    user_token=user_token,
                    storage=storage,
                    force_refresh=True,
                )
                forward_headers = _prepare_headers(request, upstream_token)
                try:
                    resp = await forward_mcp_request(body, forward_headers)
                    forward_span.set_attribute("mcp.upstream.retry_status_code", resp.status_code)
                except Exception as e:
                    forward_span.record_exception(e)
                    if tool_span:
                        tool_span.record_exception(e)
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Upstream {settings.UPSTREAM_SERVICE_NAME} MCP server unreachable on retry: {e}",
                    )
        finally:
            if tool_span_ctx:
                tool_span_ctx.__exit__(None, None, None)

    response_headers = {
        key: val
        for key, val in resp.headers.items()
        if key.lower() not in EXCLUDED_RESPONSE_HEADERS
    }

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("Content-Type", "application/json"),
    )


@router.get("/mcp")
@router.get("/mcp/")
async def mcp_get_proxy(
    request: Request,
    storage: StorageBackend = Depends(get_storage),
):
    """Support GET requests (e.g. Server-Sent Events / SSE or handshake) on MCP endpoint."""
    proxy_token, user_token = await _resolve_request_user_token(request, storage)


    upstream_token = await _resolve_and_refresh_token(
        proxy_token=proxy_token,
        user_token=user_token,
        storage=storage,
    )
    forward_headers = _prepare_headers(request, upstream_token)

    async def stream_upstream():
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "GET",
                settings.UPSTREAM_MCP_URL,
                headers=forward_headers,
                params=dict(request.query_params),
            ) as resp:
                async for chunk in resp.aiter_raw():
                    yield chunk

    return StreamingResponse(
        stream_upstream(),
        media_type="text/event-stream",
    )
