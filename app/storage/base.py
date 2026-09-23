"""Storage interface and data models for OAuth sessions, authorization codes, and user tokens."""

from abc import ABC, abstractmethod
import time
from typing import Optional
from pydantic import BaseModel, Field, AliasChoices


class OAuthSessionData(BaseModel):
    session_id: str
    google_redirect_uri: str
    google_state: str
    google_code_challenge: Optional[str] = None
    google_code_challenge_method: Optional[str] = None
    upstream_code_verifier: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("upstream_code_verifier", "metaview_code_verifier"),
    )
    created_at: float = Field(default_factory=time.time)

    @property
    def metaview_code_verifier(self) -> Optional[str]:
        return self.upstream_code_verifier


class AuthCodeData(BaseModel):
    code: str
    upstream_access_token: str = Field(
        validation_alias=AliasChoices("upstream_access_token", "metaview_access_token"),
    )
    upstream_refresh_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("upstream_refresh_token", "metaview_refresh_token"),
    )
    upstream_expires_at: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("upstream_expires_at", "metaview_expires_at"),
    )
    google_code_challenge: Optional[str] = None
    google_code_challenge_method: Optional[str] = None
    created_at: float = Field(default_factory=time.time)

    @property
    def metaview_access_token(self) -> str:
        return self.upstream_access_token

    @property
    def metaview_refresh_token(self) -> Optional[str]:
        return self.upstream_refresh_token

    @property
    def metaview_expires_at(self) -> Optional[float]:
        return self.upstream_expires_at


class UserTokenData(BaseModel):
    proxy_access_token: str
    upstream_access_token: str = Field(
        validation_alias=AliasChoices("upstream_access_token", "metaview_access_token"),
    )
    upstream_refresh_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("upstream_refresh_token", "metaview_refresh_token"),
    )
    upstream_expires_at: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("upstream_expires_at", "metaview_expires_at"),
    )
    proxy_refresh_token: Optional[str] = None
    proxy_expires_at: Optional[float] = None
    proxy_refresh_expires_at: Optional[float] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @property
    def metaview_access_token(self) -> str:
        return self.upstream_access_token

    @metaview_access_token.setter
    def metaview_access_token(self, val: str):
        self.upstream_access_token = val

    @property
    def metaview_refresh_token(self) -> Optional[str]:
        return self.upstream_refresh_token

    @metaview_refresh_token.setter
    def metaview_refresh_token(self, val: Optional[str]):
        self.upstream_refresh_token = val

    @property
    def metaview_expires_at(self) -> Optional[float]:
        return self.upstream_expires_at

    @metaview_expires_at.setter
    def metaview_expires_at(self, val: Optional[float]):
        self.upstream_expires_at = val

    def effective_proxy_expiry(self, default_lifetime_seconds: float) -> float:
        """Absolute epoch time at which the proxy access token stops being valid.

        Records written before `proxy_expires_at` existed fall back to
        `created_at + default_lifetime_seconds`. Without that fallback an older token
        would be indistinguishable from a non-expiring one and would stay usable until
        its Secret Manager TTL removed it, up to 30 days later.
        """
        if self.proxy_expires_at is not None:
            return self.proxy_expires_at
        return self.created_at + default_lifetime_seconds

    def is_proxy_token_expired(
        self, default_lifetime_seconds: float, now: Optional[float] = None
    ) -> bool:
        """Whether the proxy access token has passed its expiry."""
        current = time.time() if now is None else now
        return current >= self.effective_proxy_expiry(default_lifetime_seconds)

    def effective_refresh_expiry(self, default_lifetime_seconds: float) -> float:
        """Absolute epoch time at which the proxy refresh token stops being valid.

        Anchored to `created_at`, which is the original sign-in and is deliberately
        left untouched by the refresh grant. Re-anchoring on each refresh would let a
        session renew itself forever and never force the user back through Metaview.
        """
        if self.proxy_refresh_expires_at is not None:
            return self.proxy_refresh_expires_at
        return self.created_at + default_lifetime_seconds

    def is_proxy_refresh_expired(
        self, default_lifetime_seconds: float, now: Optional[float] = None
    ) -> bool:
        """Whether the proxy refresh token has passed its absolute expiry."""
        current = time.time() if now is None else now
        return current >= self.effective_refresh_expiry(default_lifetime_seconds)


class StorageBackend(ABC):
    """Abstract interface for token and session storage."""

    @abstractmethod
    async def save_session(self, session_id: str, data: OAuthSessionData, ttl_seconds: int = 600) -> None:
        """Save an OAuth authorization session during interactive browser login."""
        pass

    @abstractmethod
    async def get_session(self, session_id: str) -> Optional[OAuthSessionData]:
        """Retrieve an OAuth authorization session."""
        pass

    @abstractmethod
    async def delete_session(self, session_id: str) -> None:
        """Delete an OAuth authorization session once consumed."""
        pass

    @abstractmethod
    async def save_auth_code(self, code: str, data: AuthCodeData, ttl_seconds: int = 300) -> None:
        """Save a proxy authorization code minted for Gemini Enterprise."""
        pass

    @abstractmethod
    async def get_auth_code(self, code: str) -> Optional[AuthCodeData]:
        """Retrieve an authorization code."""
        pass

    @abstractmethod
    async def delete_auth_code(self, code: str) -> None:
        """Delete an authorization code once exchanged (one-time use)."""
        pass

    @abstractmethod
    async def save_user_token(self, proxy_access_token: str, data: UserTokenData, ttl_seconds: int = 86400 * 30) -> None:
        """Save an active user's proxy and Metaview tokens."""
        pass

    @abstractmethod
    async def get_user_token(self, proxy_access_token: str) -> Optional[UserTokenData]:
        """Retrieve an active user's token mapping."""
        pass

    @abstractmethod
    async def update_user_token(self, proxy_access_token: str, data: UserTokenData) -> None:
        """Update tokens when a Metaview access token is refreshed."""
        pass

    @abstractmethod
    async def delete_user_token(self, proxy_access_token: str) -> None:
        """Revoke a user's proxy session, including its refresh-token index entry."""
        pass

    @abstractmethod
    async def expire_user_token(self, proxy_access_token: str, expires_at: float) -> None:
        """Shorten an already-issued proxy access token's validity to `expires_at`.

        Used to retire a token that a refresh has superseded. Deliberately distinct
        from `update_user_token`, which also rewrites the refresh-token index: doing
        that here would point the index back at the token being retired.
        """
        pass

    @abstractmethod
    async def get_latest_user_token(self) -> Optional[UserTokenData]:
        """Retrieve the most recently active user token (for local testing console or gateway fallback)."""
        pass

    @abstractmethod
    async def get_user_token_by_refresh_token(self, refresh_token: str) -> Optional[UserTokenData]:
        """Retrieve an active user's token mapping by its proxy refresh token."""
        pass


