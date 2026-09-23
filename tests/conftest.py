"""Pytest configuration and test fixtures."""

import pytest
from httpx import AsyncClient, ASGITransport

from app.config import settings
from app.main import app
from app.storage import set_storage
from app.storage.memory import MemoryStorage


@pytest.fixture(autouse=True)
def configure_test_environment():
    """Ensure test settings and in-memory storage are active for each test."""
    settings.STORAGE_BACKEND = "memory"
    settings.GE_CLIENT_ID = "test-ge-client"
    settings.GE_CLIENT_SECRET = "test-ge-secret"
    settings.UPSTREAM_SERVICE_NAME = "testvendor"
    settings.UPSTREAM_CLIENT_ID = "test-upstream-client"
    settings.UPSTREAM_CLIENT_SECRET = "test-upstream-secret"
    settings.PROXY_BASE_URL = "https://proxy.example.com"
    settings.UPSTREAM_AUTH_URL = "https://auth.example.com/oauth2/authorize"
    settings.UPSTREAM_TOKEN_URL = "https://auth.example.com/oauth2/token"
    settings.UPSTREAM_MCP_URL = "https://mcp.example.com/mcp"
    # Set explicitly: UPSTREAM_RESOURCE has no default, so the RFC 8707 resource
    # indicator would otherwise be absent and the authorize test would stop
    # covering it silently.
    settings.UPSTREAM_RESOURCE = "https://mcp.example.com/mcp"

    memory_store = MemoryStorage()
    set_storage(memory_store)
    yield memory_store
    set_storage(None)


@pytest.fixture
async def async_client():
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://proxy.example.com") as client:
        yield client
