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
        default="ge-mcp-proxy-client",
        description="Client ID expected from Gemini Enterprise",
    )
    GE_CLIENT_SECRET: str = Field(
        default="",
        description=(
            "Client Secret expected from Gemini Enterprise. "
            "Must be set to a high-entropy random value; an empty value is "
            "rejected at startup."
        ),
    )
    GE_ALLOWED_REDIRECT_URIS: str = Field(
        default="https://vertexaisearch.cloud.google.com/oauth-redirect",
        description=(
            "Comma-separated exact-match allowlist of redirect_uri values accepted at "
            "/oauth/authorize, per RFC 6749 section 3.1.2.2. Set to '*' to disable "
            "enforcement (local development only)."
        ),
    )

    # Upstream Provider Configuration.
    #
    # Deliberately vendor-neutral: nothing here defaults to a real SaaS host.
    # A deployment that forgets to set the endpoints is caught by
    # `missing_required_settings()` at startup rather than quietly brokering
    # tokens against someone else's tenant. Per-vendor values live in deploy.sh
    # and .env.example.
    UPSTREAM_SERVICE_NAME: str = Field(
        default="upstream",
        description=(
            "Short provider identifier for this service instance, used in "
            "telemetry, secret naming and log lines (e.g. metaview, greenhouse, carta)"
        ),
    )
    UPSTREAM_CLIENT_ID: str = Field(
        default="",
        description="Client ID for the upstream OAuth application (from DCR or the vendor portal)",
    )
    UPSTREAM_CLIENT_SECRET: str = Field(
        default="",
        description=(
            "Client Secret for the upstream OAuth application. Leave empty for "
            "public PKCE clients; some providers advertise only "
            "token_endpoint_auth_method=none and reject a secret outright."
        ),
    )
    UPSTREAM_AUTH_URL: str = Field(
        default="",
        description="Upstream OAuth authorization endpoint (required)",
    )
    UPSTREAM_TOKEN_URL: str = Field(
        default="",
        description="Upstream OAuth token endpoint (required)",
    )
    UPSTREAM_MCP_URL: str = Field(
        default="",
        description="Upstream hosted MCP endpoint (required)",
    )
    UPSTREAM_REGISTRATION_URL: Optional[str] = Field(
        default=None,
        description="Optional RFC 7591 Dynamic Client Registration endpoint for automatic onboarding",
    )
    UPSTREAM_SCOPES: str = Field(
        default="openid profile email offline_access",
        description=(
            "OAuth scopes requested from the upstream provider. Providers differ "
            "widely here; read the scopes_supported list in the provider's "
            "RFC 8414 discovery document rather than assuming these defaults."
        ),
    )
    UPSTREAM_RESOURCE: Optional[str] = Field(
        default=None,
        description=(
            "RFC 8707 Resource Indicator. Set it to the upstream MCP URL when the "
            "provider requires the authorization request to name the resource."
        ),
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

    def missing_required_settings(self) -> list:
        """Names of settings that have no safe default and were left unset.

        Returned rather than raised so the caller decides the severity: the
        deployed service aborts on startup, while unit tests and the DCR helper
        script can import `settings` without a full upstream configuration.
        """
        required = {
            "UPSTREAM_CLIENT_ID": self.UPSTREAM_CLIENT_ID,
            "UPSTREAM_AUTH_URL": self.UPSTREAM_AUTH_URL,
            "UPSTREAM_TOKEN_URL": self.UPSTREAM_TOKEN_URL,
            "UPSTREAM_MCP_URL": self.UPSTREAM_MCP_URL,
            "GE_CLIENT_SECRET": self.GE_CLIENT_SECRET,
        }
        return [name for name, value in required.items() if not value]

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
            or "ge-" + os.environ.get("UPSTREAM_SERVICE_NAME", "mcp")[:4].lower()
        ),
        description=(
            "Prefix for Secret Manager secret names. Must be unique per upstream "
            "provider when several brokers share one project, otherwise two "
            "deployments would read each other's token secrets."
        ),
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
