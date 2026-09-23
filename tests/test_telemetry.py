"""Tests for OpenTelemetry distributed tracing instrumentation and context propagation."""

import pytest
from httpx import AsyncClient, ASGITransport, Response
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.main import app
from app.storage import get_storage, UserTokenData
from app.telemetry import setup_telemetry


@pytest.mark.asyncio
async def test_telemetry_mcp_spans_and_attributes(monkeypatch):
    """Verify that custom MCP spans and JSON-RPC attributes are recorded during proxy requests."""
    memory_exporter = InMemorySpanExporter()
    setup_telemetry(app, custom_exporter=memory_exporter)

    storage = get_storage()
    await storage.save_user_token(
        "test-otel-proxy-token",
        UserTokenData(
            proxy_access_token="test-otel-proxy-token",
            upstream_access_token="up-real-token-123",
            upstream_refresh_token="up-refresh-token-123",
        ),
    )

    async def mock_forward(body: bytes, headers: dict) -> Response:
        return Response(
            status_code=200,
            json={"jsonrpc": "2.0", "id": 42, "result": {"interviews": []}},
        )

    monkeypatch.setattr("app.mcp.proxy.forward_mcp_request", mock_forward)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer test-otel-proxy-token",
                "X-Cloud-Trace-Context": "105445aa7843bc8bf206b12000100000/1;o=1",
            },
            json={
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {"name": "search_interviews", "arguments": {"query": "Senior Engineer"}},
            },
        )
        assert response.status_code == 200

    spans = memory_exporter.get_finished_spans()
    span_names = [s.name for s in spans]

    assert "mcp.storage.lookup_user_token" in span_names
    assert "mcp.upstream.forward" in span_names

    forward_span = next(s for s in spans if s.name == "mcp.upstream.forward")
    assert forward_span.attributes.get("mcp.jsonrpc.method") == "tools/call"
    assert forward_span.attributes.get("mcp.tool.name") == "search_interviews"
    assert forward_span.attributes.get("mcp.upstream.status_code") == 200
