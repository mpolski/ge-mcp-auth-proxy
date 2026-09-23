"""FastAPI Application Entrypoint for Gemini Enterprise OAuth2.0 MCP Identity Broker Proxy."""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.oauth.router import router as oauth_router
from app.mcp.proxy import router as mcp_router
from app.test_ui import router as test_router
from app.storage import get_storage
from app.telemetry import setup_telemetry

# Configure structured logging
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp_proxy")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Gemini Enterprise OAuth 2.0 MCP Broker Proxy for upstream: %s", settings.UPSTREAM_SERVICE_NAME)
    logger.info("Storage backend configured: %s", settings.STORAGE_BACKEND)
    logger.info("Proxy Base URL: %s", settings.PROXY_BASE_URL)
    logger.info("Upstream MCP URL: %s", settings.UPSTREAM_MCP_URL)
    logger.info("OpenTelemetry Tracing enabled: %s", settings.OTEL_ENABLED)
    if settings.ENABLE_TEST_CONSOLE:
        logger.warning(
            "TEST CONSOLE IS ENABLED. /test exposes unauthenticated endpoints that "
            "reveal an active user's proxy access token. Never enable this in a "
            "deployed environment."
        )
    # Eagerly initialize storage backend to catch config errors on startup
    storage = get_storage()
    logger.info("Storage initialized successfully: %s", type(storage).__name__)
    yield
    logger.info("Shutting down MCP Proxy...")


app = FastAPI(
    title=f"Gemini Enterprise MCP Identity Broker Proxy ({settings.UPSTREAM_SERVICE_NAME})",
    description=(
        "Enterprise broker proxy providing per-user OAuth 2.0 3-legged authentication "
        "and MCP request translation between Gemini Enterprise and upstream MCP services "
        "(Metaview, Greenhouse, Carta, etc.)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Initialize OpenTelemetry distributed tracing & propagators
setup_telemetry(app)

# Allow CORS for browser flows
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include OAuth and MCP sub-routers
app.include_router(oauth_router)
app.include_router(mcp_router)

# The browser test console is development tooling and is mounted only on request.
# It serves unauthenticated endpoints that hand out a live user's proxy access token,
# so it must stay off anywhere the service is reachable from outside localhost.
if settings.ENABLE_TEST_CONSOLE:
    app.include_router(test_router)


@app.get("/health", tags=["Operational"])
async def health_check():
    """Liveness & readiness probe for Cloud Run."""
    return {
        "status": "healthy",
        "upstream_service": settings.UPSTREAM_SERVICE_NAME,
        "storage_backend": settings.STORAGE_BACKEND,
        "gcp_project": settings.GCP_PROJECT_ID,
        "otel_enabled": settings.OTEL_ENABLED,
    }


@app.get("/", tags=["Operational"])
async def root():
    """Service information and endpoint directory."""
    base = settings.PROXY_BASE_URL.rstrip("/")
    endpoints = {
        "oauth_authorize": f"{base}/oauth/authorize",
        "oauth_callback": f"{base}/oauth/callback",
        "oauth_token": f"{base}/oauth/token",
        "oauth_revoke": f"{base}/oauth/revoke",
        "mcp_proxy": f"{base}/mcp",
        "health": f"{base}/health",
    }
    if settings.ENABLE_TEST_CONSOLE:
        endpoints["test_console"] = f"{base}/test"
    return {
        "service": f"Gemini Enterprise OAuth 2.0 MCP Identity Broker ({settings.UPSTREAM_SERVICE_NAME})",
        "upstream_service": settings.UPSTREAM_SERVICE_NAME,
        "upstream_mcp_url": settings.UPSTREAM_MCP_URL,
        "endpoints": endpoints,
        "status": "running",
    }


@app.get("/.well-known/oauth-authorization-server", tags=["OAuth 2.0 Metadata"])
async def oauth_metadata():
    """RFC 8414 OAuth 2.0 Authorization Server Metadata."""
    base = settings.PROXY_BASE_URL.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
        ],
        "revocation_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
        ],
        # Only S256 is advertised. Advertising "plain" would let a client negotiate
        # a downgrade in which the verifier is sent in cleartext at authorize time,
        # which defeats the point of PKCE. The verifier check itself still accepts
        # "plain" for spec compliance if a challenge was somehow stored that way.
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["offline_access"],
    }
