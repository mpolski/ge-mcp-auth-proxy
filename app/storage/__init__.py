"""Storage package initialization and factory."""

from typing import Optional
from app.config import settings
from app.storage.base import StorageBackend, OAuthSessionData, AuthCodeData, UserTokenData
from app.storage.memory import MemoryStorage

_storage_instance: Optional[StorageBackend] = None


def get_storage() -> StorageBackend:
    """Return the configured storage backend singleton."""
    global _storage_instance
    if _storage_instance is None:
        if settings.STORAGE_BACKEND == "secret_manager":
            from app.storage.secret_manager import SecretManagerStorage
            if not settings.GCP_PROJECT_ID:
                # Fall back to MemoryStorage if project ID is not configured (e.g. local dev without GCP)
                return MemoryStorage()
            _storage_instance = SecretManagerStorage(
                project_id=settings.GCP_PROJECT_ID,
                prefix=settings.SECRET_PREFIX,
            )
        else:
            _storage_instance = MemoryStorage()
    return _storage_instance


def set_storage(storage: Optional[StorageBackend]) -> None:
    """Set the storage backend singleton (useful for testing)."""
    global _storage_instance
    _storage_instance = storage


__all__ = [
    "StorageBackend",
    "OAuthSessionData",
    "AuthCodeData",
    "UserTokenData",
    "MemoryStorage",
    "get_storage",
    "set_storage",
]
