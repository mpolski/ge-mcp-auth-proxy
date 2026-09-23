"""Google Cloud Secret Manager storage backend for sessions, codes, and tokens."""

import asyncio
import hashlib
import logging
import re
import time
from typing import Optional, Dict
from google.api_core.exceptions import NotFound, AlreadyExists, FailedPrecondition
from google.cloud import secretmanager_v1 as sm
from google.protobuf.duration_pb2 import Duration

from app.storage.base import (
    StorageBackend,
    OAuthSessionData,
    AuthCodeData,
    UserTokenData,
)
from app.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)


class SecretManagerStorage(StorageBackend):
    """Google Cloud Secret Manager implementation for encrypted, compliant token storage.
    Leverages Secret Manager's native TTL for automated expiration and deletion of ephemeral data.
    """

    def __init__(self, project_id: str, prefix: str = "ge-mcp"):
        if not project_id:
            raise ValueError("GCP Project ID must be provided for SecretManagerStorage")
        self.project_id = project_id
        self.prefix = prefix
        self.client = sm.SecretManagerServiceClient()

    def _sanitize_key(self, key: str) -> str:
        """Ensure key contains only valid Secret Manager characters [a-zA-Z0-9_-] and stays under 200 chars."""
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", key)
        if len(sanitized) > 180:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
            sanitized = f"{sanitized[:140]}-{digest}"
        return sanitized

    def _secret_id(self, category: str, key: str) -> str:
        return f"{self.prefix}-{category}-{self._sanitize_key(key)}"

    def _sync_write_secret(
        self,
        secret_id: str,
        payload_str: str,
        ttl_seconds: Optional[int] = None,
        labels: Optional[Dict[str, str]] = None,
        prune_old_versions: bool = False,
    ) -> None:
        with tracer.start_as_current_span("secret_manager.write_secret") as span:
            span.set_attribute("secret.prefix", self.prefix)
            if labels and "type" in labels:
                span.set_attribute("secret.type", labels["type"])
            parent = f"projects/{self.project_id}"
            secret_path = f"{parent}/secrets/{secret_id}"
            payload_bytes = payload_str.encode("utf-8")

            secret_labels = {"app": "ge-mcp-auth-proxy"}
            if labels:
                secret_labels.update(labels)

            # Create secret if it does not already exist
            secret_proto = sm.Secret(
                replication=sm.Replication(automatic=sm.Replication.Automatic()),
                labels=secret_labels,
            )
            if ttl_seconds and ttl_seconds > 0:
                secret_proto.ttl = Duration(seconds=ttl_seconds)

            try:
                self.client.create_secret(
                    request={
                        "parent": parent,
                        "secret_id": secret_id,
                        "secret": secret_proto,
                    }
                )
            except AlreadyExists:
                pass  # Secret already created, proceed to add new version

            # Add payload version
            new_version = self.client.add_secret_version(
                request={
                    "parent": secret_path,
                    "payload": {"data": payload_bytes},
                }
            )

            if prune_old_versions:
                self._sync_destroy_prior_versions(secret_path, keep=new_version.name)

    def _sync_destroy_prior_versions(self, secret_path: str, keep: str) -> None:
        """Destroy every enabled version of a secret except the one just written.

        Secret Manager bills per *active secret version*. Token payloads are rewritten
        on every upstream refresh, so without this the superseded versions stay active
        (and billable) until the parent secret's TTL expires. Only the newest version is
        ever read back, via the `latest` alias, so older ones carry no value.
        """
        with tracer.start_as_current_span("secret_manager.destroy_prior_versions") as span:
            destroyed = 0
            try:
                versions = self.client.list_secret_versions(
                    request={"parent": secret_path, "filter": "state:ENABLED"}
                )
                for version in versions:
                    if version.name == keep:
                        continue
                    try:
                        self.client.destroy_secret_version(request={"name": version.name})
                        destroyed += 1
                    except (NotFound, FailedPrecondition):
                        # Already destroyed or concurrently removed - nothing to do.
                        pass
            except Exception as e:
                # Pruning is a cost optimisation, never a correctness requirement:
                # a failure here must not break the caller's token write.
                span.record_exception(e)
                logger.warning("Failed to prune old versions of %s: %s", secret_path, e)
            span.set_attribute("secret.versions_destroyed", destroyed)

    def _sync_read_secret(self, secret_id: str) -> Optional[str]:
        with tracer.start_as_current_span("secret_manager.read_secret") as span:
            span.set_attribute("secret.id", secret_id)
            version_path = f"projects/{self.project_id}/secrets/{secret_id}/versions/latest"
            try:
                response = self.client.access_secret_version(request={"name": version_path})
                span.set_attribute("secret.found", True)
                return response.payload.data.decode("utf-8")
            except NotFound:
                span.set_attribute("secret.found", False)
                return None
            except Exception as e:
                span.record_exception(e)
                logger.warning("Failed to access secret %s: %s", secret_id, e)
                return None

    def _sync_delete_secret(self, secret_id: str) -> None:
        with tracer.start_as_current_span("secret_manager.delete_secret") as span:
            span.set_attribute("secret.id", secret_id)
            secret_path = f"projects/{self.project_id}/secrets/{secret_id}"
            try:
                self.client.delete_secret(request={"name": secret_path})
                span.set_attribute("secret.deleted", True)
            except NotFound:
                span.set_attribute("secret.deleted", False)
            except Exception as e:
                span.record_exception(e)
                logger.warning("Failed to delete secret %s: %s", secret_id, e)

    async def save_session(self, session_id: str, data: OAuthSessionData, ttl_seconds: int = 600) -> None:
        sec_id = self._secret_id("sess", session_id)
        await asyncio.to_thread(
            self._sync_write_secret,
            sec_id,
            data.model_dump_json(),
            ttl_seconds,
            {"type": "oauth-session"},
        )

    async def get_session(self, session_id: str) -> Optional[OAuthSessionData]:
        sec_id = self._secret_id("sess", session_id)
        raw = await asyncio.to_thread(self._sync_read_secret, sec_id)
        if not raw:
            return None
        try:
            return OAuthSessionData.model_validate_json(raw)
        except Exception as e:
            logger.error("Error parsing session data for %s: %s", session_id, e)
            return None

    async def delete_session(self, session_id: str) -> None:
        sec_id = self._secret_id("sess", session_id)
        await asyncio.to_thread(self._sync_delete_secret, sec_id)

    async def save_auth_code(self, code: str, data: AuthCodeData, ttl_seconds: int = 300) -> None:
        sec_id = self._secret_id("code", code)
        await asyncio.to_thread(
            self._sync_write_secret,
            sec_id,
            data.model_dump_json(),
            ttl_seconds,
            {"type": "auth-code"},
        )

    async def get_auth_code(self, code: str) -> Optional[AuthCodeData]:
        sec_id = self._secret_id("code", code)
        raw = await asyncio.to_thread(self._sync_read_secret, sec_id)
        if not raw:
            return None
        try:
            return AuthCodeData.model_validate_json(raw)
        except Exception as e:
            logger.error("Error parsing auth code data for %s: %s", code, e)
            return None

    async def delete_auth_code(self, code: str) -> None:
        sec_id = self._secret_id("code", code)
        await asyncio.to_thread(self._sync_delete_secret, sec_id)

    async def save_user_token(self, proxy_access_token: str, data: UserTokenData, ttl_seconds: int = 86400 * 30) -> None:
        sec_id = self._secret_id("tok", proxy_access_token)
        payload = data.model_dump_json()
        await asyncio.to_thread(
            self._sync_write_secret,
            sec_id,
            payload,
            ttl_seconds,
            {"type": "user-token"},
            True,
        )
        if data.proxy_refresh_token:
            # Keyed by the refresh token, which is stable across refreshes, so this
            # secret is rewritten repeatedly and must have its old versions pruned.
            ref_id = self._secret_id("ref", data.proxy_refresh_token)
            await asyncio.to_thread(
                self._sync_write_secret,
                ref_id,
                payload,
                ttl_seconds,
                {"type": "refresh-token"},
                True,
            )

    async def get_user_token(self, proxy_access_token: str) -> Optional[UserTokenData]:
        sec_id = self._secret_id("tok", proxy_access_token)
        raw = await asyncio.to_thread(self._sync_read_secret, sec_id)
        if not raw:
            return None
        try:
            return UserTokenData.model_validate_json(raw)
        except Exception as e:
            logger.error("Error parsing user token data: %s", e)
            return None

    async def update_user_token(self, proxy_access_token: str, data: UserTokenData) -> None:
        sec_id = self._secret_id("tok", proxy_access_token)
        payload = data.model_dump_json()
        # Update payload by adding a new secret version, then destroying the previous
        # one so a long-lived session does not accrue hundreds of billable versions.
        await asyncio.to_thread(
            self._sync_write_secret,
            sec_id,
            payload,
            None,
            {"type": "user-token"},
            True,
        )
        if data.proxy_refresh_token:
            ref_id = self._secret_id("ref", data.proxy_refresh_token)
            await asyncio.to_thread(
                self._sync_write_secret,
                ref_id,
                payload,
                None,
                {"type": "refresh-token"},
                True,
            )

    def _sync_delete_user_token(self, proxy_access_token: str) -> None:
        """Delete both secrets that make up a session.

        `save_user_token` writes the same payload twice: once keyed by the access
        token and once keyed by the refresh token as a lookup index. Deleting only the
        first would leave the index behind, and the index on its own is enough to
        resolve a request at /mcp, so the session would survive its own revocation.
        """
        tok_id = self._secret_id("tok", proxy_access_token)
        raw = self._sync_read_secret(tok_id)
        self._sync_delete_secret(tok_id)

        if not raw:
            return
        try:
            record = UserTokenData.model_validate_json(raw)
        except Exception as e:
            logger.warning("Could not parse token record while revoking: %s", e)
            return
        if record.proxy_refresh_token:
            self._sync_delete_secret(self._secret_id("ref", record.proxy_refresh_token))

    async def delete_user_token(self, proxy_access_token: str) -> None:
        await asyncio.to_thread(self._sync_delete_user_token, proxy_access_token)

    def _sync_expire_user_token(self, proxy_access_token: str, expires_at: float) -> None:
        tok_id = self._secret_id("tok", proxy_access_token)
        raw = self._sync_read_secret(tok_id)
        if not raw:
            return
        try:
            record = UserTokenData.model_validate_json(raw)
        except Exception as e:
            logger.warning("Could not parse token record while expiring: %s", e)
            return

        record.proxy_expires_at = expires_at
        record.updated_at = time.time()
        # Only the access-token secret is rewritten. The `ref` index must keep pointing
        # at the token that replaced this one.
        self._sync_write_secret(
            tok_id,
            record.model_dump_json(),
            None,
            {"type": "user-token"},
            True,
        )

    async def expire_user_token(self, proxy_access_token: str, expires_at: float) -> None:
        await asyncio.to_thread(self._sync_expire_user_token, proxy_access_token, expires_at)

    def _sync_get_latest_user_token(self) -> Optional[UserTokenData]:
        parent = f"projects/{self.project_id}"
        prefix = f"{self.prefix}-tok-"
        try:
            secrets = self.client.list_secrets(request={"parent": parent})
            latest_data: Optional[UserTokenData] = None
            latest_time: float = 0.0
            for s in secrets:
                secret_id = s.name.split("/")[-1]
                if secret_id.startswith(prefix):
                    val = self._sync_read_secret(secret_id)
                    if val:
                        try:
                            token_data = UserTokenData.model_validate_json(val)
                            if token_data.created_at > latest_time:
                                latest_time = token_data.created_at
                                latest_data = token_data
                        except Exception:
                            pass
            return latest_data
        except Exception as e:
            logger.warning("Failed to list secrets for latest token: %s", e)
            return None

    async def get_latest_user_token(self) -> Optional[UserTokenData]:
        return await asyncio.to_thread(self._sync_get_latest_user_token)

    def _sync_get_user_token_by_refresh_token(self, refresh_token: str) -> Optional[UserTokenData]:
        # Fast path: check {prefix}-ref-{refresh_token}
        ref_id = self._secret_id("ref", refresh_token)
        raw = self._sync_read_secret(ref_id)
        if raw:
            try:
                return UserTokenData.model_validate_json(raw)
            except Exception:
                pass

        # Fallback: scan {prefix}-tok-* secrets
        parent = f"projects/{self.project_id}"
        prefix = f"{self.prefix}-tok-"
        try:
            secrets = self.client.list_secrets(request={"parent": parent})
            for s in secrets:
                secret_id = s.name.split("/")[-1]
                if secret_id.startswith(prefix):
                    val = self._sync_read_secret(secret_id)
                    if val:
                        try:
                            token_data = UserTokenData.model_validate_json(val)
                            if token_data.proxy_refresh_token == refresh_token:
                                return token_data
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("Failed to scan secrets for refresh token: %s", e)
        return None

    async def get_user_token_by_refresh_token(self, refresh_token: str) -> Optional[UserTokenData]:
        return await asyncio.to_thread(self._sync_get_user_token_by_refresh_token, refresh_token)

