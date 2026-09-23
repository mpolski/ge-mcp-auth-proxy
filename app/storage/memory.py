"""In-memory storage backend with TTL expiration support for local development and testing."""

import asyncio
import time
from typing import Optional, Dict, Tuple
from app.storage.base import (
    StorageBackend,
    OAuthSessionData,
    AuthCodeData,
    UserTokenData,
)


class MemoryStorage(StorageBackend):
    """In-memory store for sessions, codes, and tokens.
    Entries are stored as (data, expiry_timestamp).
    """

    def __init__(self):
        self._sessions: Dict[str, Tuple[OAuthSessionData, float]] = {}
        self._auth_codes: Dict[str, Tuple[AuthCodeData, float]] = {}
        self._user_tokens: Dict[str, Tuple[UserTokenData, float]] = {}
        self._lock = asyncio.Lock()

    async def save_session(self, session_id: str, data: OAuthSessionData, ttl_seconds: int = 600) -> None:
        async with self._lock:
            expiry = time.time() + ttl_seconds
            self._sessions[session_id] = (data, expiry)

    async def get_session(self, session_id: str) -> Optional[OAuthSessionData]:
        async with self._lock:
            entry = self._sessions.get(session_id)
            if not entry:
                return None
            data, expiry = entry
            if time.time() > expiry:
                del self._sessions[session_id]
                return None
            return data

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def save_auth_code(self, code: str, data: AuthCodeData, ttl_seconds: int = 300) -> None:
        async with self._lock:
            expiry = time.time() + ttl_seconds
            self._auth_codes[code] = (data, expiry)

    async def get_auth_code(self, code: str) -> Optional[AuthCodeData]:
        async with self._lock:
            entry = self._auth_codes.get(code)
            if not entry:
                return None
            data, expiry = entry
            if time.time() > expiry:
                del self._auth_codes[code]
                return None
            return data

    async def delete_auth_code(self, code: str) -> None:
        async with self._lock:
            self._auth_codes.pop(code, None)

    async def save_user_token(self, proxy_access_token: str, data: UserTokenData, ttl_seconds: int = 86400 * 30) -> None:
        async with self._lock:
            expiry = time.time() + ttl_seconds
            # Stored as a copy. The Secret Manager backend serialises to JSON, so a
            # caller there can never reach back into stored state; without copying here
            # the two backends would diverge. Concretely, the refresh grant mutates the
            # record it just read and saves it under a new key - with shared references
            # the old key would see those mutations too, and retiring the superseded
            # token would retire its replacement along with it.
            self._user_tokens[proxy_access_token] = (data.model_copy(deep=True), expiry)

    async def get_user_token(self, proxy_access_token: str) -> Optional[UserTokenData]:
        async with self._lock:
            entry = self._user_tokens.get(proxy_access_token)
            if not entry:
                return None
            data, expiry = entry
            if time.time() > expiry:
                del self._user_tokens[proxy_access_token]
                return None
            return data.model_copy(deep=True)

    async def update_user_token(self, proxy_access_token: str, data: UserTokenData) -> None:
        async with self._lock:
            entry = self._user_tokens.get(proxy_access_token)
            expiry = entry[1] if entry else (time.time() + 86400 * 30)
            data.updated_at = time.time()
            self._user_tokens[proxy_access_token] = (data.model_copy(deep=True), expiry)

    async def delete_user_token(self, proxy_access_token: str) -> None:
        async with self._lock:
            entry = self._user_tokens.pop(proxy_access_token, None)
            if not entry:
                return
            # Superseded access tokens from earlier refreshes share this refresh token
            # and are still reachable via get_user_token_by_refresh_token, which scans
            # every record. Leaving them would let a revoked session keep resolving.
            refresh_token = entry[0].proxy_refresh_token
            if not refresh_token:
                return
            for key in [
                k for k, (data, _) in self._user_tokens.items()
                if data.proxy_refresh_token == refresh_token
            ]:
                del self._user_tokens[key]

    async def expire_user_token(self, proxy_access_token: str, expires_at: float) -> None:
        async with self._lock:
            entry = self._user_tokens.get(proxy_access_token)
            if not entry:
                return
            data, expiry = entry
            data.proxy_expires_at = expires_at
            data.updated_at = time.time()
            self._user_tokens[proxy_access_token] = (data, expiry)

    async def get_latest_user_token(self) -> Optional[UserTokenData]:
        async with self._lock:
            active_tokens = [
                data for data, expiry in self._user_tokens.values()
                if time.time() <= expiry
            ]
            if not active_tokens:
                return None
            return max(active_tokens, key=lambda t: t.created_at).model_copy(deep=True)

    async def get_user_token_by_refresh_token(self, refresh_token: str) -> Optional[UserTokenData]:
        async with self._lock:
            for data, expiry in self._user_tokens.values():
                if time.time() <= expiry and data.proxy_refresh_token == refresh_token:
                    return data.model_copy(deep=True)
            return None


