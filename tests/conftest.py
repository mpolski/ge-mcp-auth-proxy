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
    settings.METAVIEW_CLIENT_ID = "test-mv-client"
    settings.METAVIEW_CLIENT_SECRET = "test-mv-secret"
    settings.PROXY_BASE_URL = "https://proxy.example.com"
    settings.METAVIEW_AUTH_URL = "https://auth.metaview.ai/oauth2/authorize"
    settings.METAVIEW_TOKEN_URL = "https://auth.metaview.ai/oauth2/token"
    settings.METAVIEW_MCP_URL = "https://mcp.metaview.ai/mcp"

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
