"""Configuration settings for the Gemini Enterprise OAuth 2.0 MCP Identity Broker."""

import os
from typing import Literal, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Proxy public URL (must be set in deployment)
    PROXY_BASE_URL: str = Field(
        default="http://localhost:8080",
        description="Publicly accessible base URL for this proxy",
    )

    # Gemini Enterprise OAuth Credentials
    GE_CLIENT_ID: str = Field(
        default="ge-metaview-client",
        description="Client ID expected from Gemini Enterprise / Discovery Engine",
    )
    GE_CLIENT_SECRET: str = Field(
        default="ge-metaview-secret-placeholder",
        description="Client Secret expected from Gemini Enterprise / Discovery Engine",
    )
    GE_ALLOWED_REDIRECT_URIS: str = Field(
        default="https://vertexaisearch.cloud.google.com/oauth-redirect",
        description=(
            "Comma-separated exact-match allowlist of redirect_uri values accepted at "
            "/oauth/authorize, per RFC 6749 section 3.1.2.2. Set to '*' to disable "
            "enforcement (local development only)."
        ),
    )

    # Upstream Provider Configuration (Metaview, Greenhouse, Carta, etc.)
    UPSTREAM_SERVICE_NAME: str = Field(
        default="metaview",
        description="Provider identifier (e.g. metaview, greenhouse, carta), used in telemetry and defaults",
    )
    UPSTREAM_CLIENT_ID: str = Field(
        default_factory=lambda: (
            os.environ.get("UPSTREAM_CLIENT_ID")
            or os.environ.get("METAVIEW_CLIENT_ID")
            or "upstream-client-id-placeholder"
        ),
        description="Client ID for upstream OAuth application (from DCR or portal)",
    )
    UPSTREAM_CLIENT_SECRET: str = Field(
        default_factory=lambda: (
            os.environ.get("UPSTREAM_CLIENT_SECRET")
            or os.environ.get("METAVIEW_CLIENT_SECRET")
            or ""
        ),
        description="Client Secret for upstream OAuth application (if confidential client)",
    )
    UPSTREAM_AUTH_URL: str = Field(
        default_factory=lambda: (
            os.environ.get("UPSTREAM_AUTH_URL")
            or os.environ.get("METAVIEW_AUTH_URL")
            or "https://auth.metaview.ai/oauth2/authorize"
        ),
        description="Upstream OAuth authorization endpoint",
    )
    UPSTREAM_TOKEN_URL: str = Field(
        default_factory=lambda: (
            os.environ.get("UPSTREAM_TOKEN_URL")
            or os.environ.get("METAVIEW_TOKEN_URL")
            or "https://auth.metaview.ai/oauth2/token"
        ),
        description="Upstream OAuth token endpoint",
    )
    UPSTREAM_MCP_URL: str = Field(
        default_factory=lambda: (
            os.environ.get("UPSTREAM_MCP_URL")
            or os.environ.get("METAVIEW_MCP_URL")
            or "https://mcp.metaview.ai/mcp"
        ),
        description="Upstream hosted MCP endpoint",
    )
    UPSTREAM_REGISTRATION_URL: Optional[str] = Field(
        default=None,
        description="Optional RFC 7591 Dynamic Client Registration endpoint for automatic onboarding",
    )
    UPSTREAM_SCOPES: str = Field(
        default="openid profile email offline_access",
        description="OAuth scopes requested from the upstream provider",
    )
    UPSTREAM_RESOURCE: Optional[str] = Field(
        default_factory=lambda: (
            os.environ.get("UPSTREAM_RESOURCE")
            or os.environ.get("METAVIEW_MCP_URL")
            or "https://mcp.metaview.ai/mcp"
        ),
        description="RFC 8707 Resource Indicator (if required by upstream, e.g. Metaview)",
    )
    UPSTREAM_AUDIENCE: Optional[str] = Field(
        default=None,
        description="OAuth audience parameter (if required by upstream, e.g. Auth0/Okta backed services)",
    )

    @property
    def allowed_redirect_uris(self) -> list:
        """Parsed allowlist of redirect URIs accepted at /oauth/authorize.

        An empty list means enforcement is disabled, which is also what a bare '*'
        signals. Both are reported at startup so an unrestricted deployment is never
        silent.
        """
        return [u.strip() for u in self.GE_ALLOWED_REDIRECT_URIS.split(",") if u.strip()]

    @property
    def redirect_uri_enforcement_enabled(self) -> bool:
        allowed = self.allowed_redirect_uris
        return bool(allowed) and "*" not in allowed

    # Backwards compatibility properties for existing scripts/tests
    @property
    def METAVIEW_CLIENT_ID(self) -> str:
        return self.UPSTREAM_CLIENT_ID

    @METAVIEW_CLIENT_ID.setter
    def METAVIEW_CLIENT_ID(self, val: str):
        self.UPSTREAM_CLIENT_ID = val

    @property
    def METAVIEW_CLIENT_SECRET(self) -> str:
        return self.UPSTREAM_CLIENT_SECRET

    @METAVIEW_CLIENT_SECRET.setter
    def METAVIEW_CLIENT_SECRET(self, val: str):
        self.UPSTREAM_CLIENT_SECRET = val

    @property
    def METAVIEW_AUTH_URL(self) -> str:
        return self.UPSTREAM_AUTH_URL

    @METAVIEW_AUTH_URL.setter
    def METAVIEW_AUTH_URL(self, val: str):
        self.UPSTREAM_AUTH_URL = val

    @property
    def METAVIEW_TOKEN_URL(self) -> str:
        return self.UPSTREAM_TOKEN_URL

    @METAVIEW_TOKEN_URL.setter
    def METAVIEW_TOKEN_URL(self, val: str):
        self.UPSTREAM_TOKEN_URL = val

    @property
    def METAVIEW_MCP_URL(self) -> str:
        return self.UPSTREAM_MCP_URL

    @METAVIEW_MCP_URL.setter
    def METAVIEW_MCP_URL(self, val: str):
        self.UPSTREAM_MCP_URL = val

    # Google Cloud Project & Secret Manager Configuration
    GCP_PROJECT_ID: Optional[str] = Field(
        default_factory=lambda: (
            os.environ.get("GCP_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
        ),
        description="GCP Project ID for Secret Manager",
    )
    GCP_PROJECT_NUMBER: Optional[str] = Field(
        default=None,
        description=(
            "GCP Project Number, used to build the Agent Registry MCP server URN for "
            "telemetry. Resolved at deploy time; tracing degrades gracefully if unset."
        ),
    )
    GCP_REGION: str = Field(
        default="us-central1",
        description="GCP region this service is deployed to (used in the telemetry URN)",
    )
    SECRET_PREFIX: str = Field(
        default_factory=lambda: (
            os.environ.get("SECRET_PREFIX")
            or ("ge-mv" if os.environ.get("UPSTREAM_SERVICE_NAME", "metaview") == "metaview" else f"ge-{os.environ.get('UPSTREAM_SERVICE_NAME')}")
        ),
        description="Prefix for Secret Manager secret names",
    )
    STORAGE_BACKEND: Literal["secret_manager", "memory"] = Field(
        default="memory" if os.environ.get("TESTING") else "secret_manager",
        description="Storage backend for sessions, codes, and tokens",
    )

    # TTLs in seconds
    SESSION_TTL_SECONDS: int = Field(
        default=600,
        description="OAuth interactive sign-in session TTL (10 minutes)",
    )
    AUTH_CODE_TTL_SECONDS: int = Field(
        default=300,
        description="Proxy Authorization Code TTL (5 minutes)",
    )
    PROXY_ACCESS_TOKEN_EXPIRES_IN: int = Field(
        default=3600,
        description="Gemini Enterprise proxy access token validity (1 hour)",
    )
    PROXY_REFRESH_TOKEN_EXPIRES_IN: int = Field(
        default=86400 * 30,
        description=(
            "Absolute proxy refresh token validity (30 days). Measured from the "
            "original sign-in, not from the last refresh, so a session cannot be "
            "extended indefinitely. Matches the Secret Manager TTL on token secrets."
        ),
    )
    PROXY_TOKEN_REVOCATION_GRACE_SECONDS: int = Field(
        default=60,
        description=(
            "How long a proxy access token superseded by a refresh stays usable. "
            "Without a grace window, requests already in flight when Gemini "
            "Enterprise refreshes would fail; with one, the old token is retired "
            "shortly after instead of remaining valid for its full lifetime."
        ),
    )

    # Server & Observability settings
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    DEBUG: bool = False
    OTEL_ENABLED: bool = Field(
        default=False if os.environ.get("TESTING") else True,
        description="Enable OpenTelemetry distributed tracing with Google Cloud Trace",
    )

    # Local development tooling
    ENABLE_TEST_CONSOLE: bool = Field(
        default=False,
        description=(
            "Mount the interactive /test browser console. MUST remain False in any "
            "deployed environment: the console exposes unauthenticated endpoints that "
            "reveal an active user's proxy access token. Intended for localhost only."
        ),
    )



# Global settings singleton
settings = Settings()
