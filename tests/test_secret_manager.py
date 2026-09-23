"""Unit tests for Google Cloud Secret Manager storage implementation."""

import time
from unittest.mock import MagicMock, patch
import pytest
from google.api_core.exceptions import NotFound, AlreadyExists
from app.storage.base import OAuthSessionData, AuthCodeData, UserTokenData
from app.storage.secret_manager import SecretManagerStorage


@pytest.fixture
def mock_sm_client():
    with patch("google.cloud.secretmanager_v1.SecretManagerServiceClient") as mock_cls:
        client_inst = MagicMock()
        mock_cls.return_value = client_inst
        yield client_inst


@pytest.mark.asyncio
async def test_secret_manager_save_and_get_session(mock_sm_client):
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")

    session = OAuthSessionData(
        session_id="sess-abc-123",
        google_redirect_uri="https://vertexaisearch.cloud.google.com/oauth-redirect",
        google_state="state-xyz",
    )

    # Test save_session
    await storage.save_session("sess-abc-123", session, ttl_seconds=600)
    assert mock_sm_client.create_secret.called
    assert mock_sm_client.add_secret_version.called

    # Test get_session
    mock_payload = MagicMock()
    mock_payload.data = session.model_dump_json().encode("utf-8")
    mock_sm_client.access_secret_version.return_value.payload = mock_payload

    retrieved = await storage.get_session("sess-abc-123")
    assert retrieved is not None
    assert retrieved.session_id == "sess-abc-123"
    assert retrieved.google_state == "state-xyz"


@pytest.mark.asyncio
async def test_secret_manager_already_exists_on_create(mock_sm_client):
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")
    mock_sm_client.create_secret.side_effect = AlreadyExists("Secret already exists")

    session = OAuthSessionData(
        session_id="sess-exist",
        google_redirect_uri="https://example.com",
        google_state="s",
    )
    # Should not raise AlreadyExists error, should proceed to add_secret_version
    await storage.save_session("sess-exist", session)
    assert mock_sm_client.add_secret_version.called


@pytest.mark.asyncio
async def test_secret_manager_get_not_found(mock_sm_client):
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")
    mock_sm_client.access_secret_version.side_effect = NotFound("Secret not found")

    retrieved = await storage.get_session("non-existent")
    assert retrieved is None


@pytest.mark.asyncio
async def test_secret_manager_delete(mock_sm_client):
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")
    await storage.delete_session("sess-to-del")
    assert mock_sm_client.delete_secret.called


@pytest.mark.asyncio
async def test_secret_manager_update_user_token(mock_sm_client):
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")
    user_tok = UserTokenData(
        proxy_access_token="tok-1",
        upstream_access_token="up-1",
    )
    await storage.update_user_token("tok-1", user_tok)
    assert mock_sm_client.add_secret_version.called


def _version(name: str):
    v = MagicMock()
    v.name = name
    return v


@pytest.mark.asyncio
async def test_update_user_token_destroys_superseded_versions(mock_sm_client):
    """Superseded versions must be destroyed so they stop being billable.

    Secret Manager charges per *active* secret version. Token payloads are rewritten on
    every upstream refresh, so without pruning a single long-lived session accumulates
    hundreds of active versions before the parent secret's TTL removes them.
    """
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")

    secret_path = "projects/test-project/secrets/ge-test-tok-tok-1"
    new_version = _version(f"{secret_path}/versions/3")
    mock_sm_client.add_secret_version.return_value = new_version
    mock_sm_client.list_secret_versions.return_value = [
        _version(f"{secret_path}/versions/1"),
        _version(f"{secret_path}/versions/2"),
        new_version,
    ]

    await storage.update_user_token(
        "tok-1",
        UserTokenData(proxy_access_token="tok-1", upstream_access_token="up-1"),
    )

    destroyed = {
        call.kwargs["request"]["name"]
        for call in mock_sm_client.destroy_secret_version.call_args_list
    }
    assert destroyed == {
        f"{secret_path}/versions/1",
        f"{secret_path}/versions/2",
    }
    # The version just written must survive - it is what `latest` resolves to.
    assert new_version.name not in destroyed


@pytest.mark.asyncio
async def test_ephemeral_secrets_are_not_pruned(mock_sm_client):
    """Sessions and auth codes use a fresh secret per value, so pruning is wasted work."""
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")

    await storage.save_session(
        "sess-1",
        OAuthSessionData(
            session_id="sess-1",
            google_redirect_uri="https://example.com",
            google_state="s",
        ),
    )
    await storage.save_auth_code(
        "code-1",
        AuthCodeData(code="code-1", upstream_access_token="up-1"),
    )

    assert not mock_sm_client.list_secret_versions.called
    assert not mock_sm_client.destroy_secret_version.called


@pytest.mark.asyncio
async def test_pruning_failure_does_not_break_token_write(mock_sm_client):
    """Pruning is a cost optimisation and must never fail the caller's write."""
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")
    mock_sm_client.list_secret_versions.side_effect = Exception("quota exceeded")

    await storage.update_user_token(
        "tok-1",
        UserTokenData(proxy_access_token="tok-1", upstream_access_token="up-1"),
    )

    assert mock_sm_client.add_secret_version.called


def _stub_read(mock_sm_client, record: UserTokenData):
    """Make every access_secret_version call return `record`."""
    payload = MagicMock()
    payload.data = record.model_dump_json().encode("utf-8")
    mock_sm_client.access_secret_version.return_value.payload = payload


@pytest.mark.asyncio
async def test_delete_user_token_also_removes_refresh_index(mock_sm_client):
    """Revocation must remove both secrets that make up a session.

    A session is written twice: keyed by access token, and keyed by refresh token as a
    lookup index. Deleting only the first leaves the index behind, and the index alone
    is enough to resolve a request at /mcp, so the session would survive revocation.
    """
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")
    _stub_read(
        mock_sm_client,
        UserTokenData(
            proxy_access_token="tok-1",
            upstream_access_token="up-1",
            proxy_refresh_token="ref-1",
        ),
    )

    await storage.delete_user_token("tok-1")

    deleted = {
        call.kwargs["request"]["name"]
        for call in mock_sm_client.delete_secret.call_args_list
    }
    assert deleted == {
        "projects/test-project/secrets/ge-test-tok-tok-1",
        "projects/test-project/secrets/ge-test-ref-ref-1",
    }


@pytest.mark.asyncio
async def test_expire_user_token_leaves_refresh_index_alone(mock_sm_client):
    """Retiring a superseded token must not rewrite the refresh-token index.

    The index has already been repointed at the replacement token by that stage.
    Rewriting it here - which `update_user_token` would do - would point it back at the
    token being retired and undo the rotation.
    """
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")
    _stub_read(
        mock_sm_client,
        UserTokenData(
            proxy_access_token="tok-old",
            upstream_access_token="up-1",
            proxy_refresh_token="ref-1",
            proxy_expires_at=time.time() + 3600,
        ),
    )

    deadline = time.time() + 60
    await storage.expire_user_token("tok-old", deadline)

    written_to = [
        call.kwargs["request"]["parent"]
        for call in mock_sm_client.add_secret_version.call_args_list
    ]
    assert written_to == ["projects/test-project/secrets/ge-test-tok-tok-old"]

    payload = mock_sm_client.add_secret_version.call_args.kwargs["request"]["payload"]
    persisted = UserTokenData.model_validate_json(payload["data"].decode("utf-8"))
    assert persisted.proxy_expires_at == deadline


@pytest.mark.asyncio
async def test_expire_user_token_on_missing_secret_is_a_noop(mock_sm_client):
    """A token already gone from storage needs no retirement."""
    storage = SecretManagerStorage(project_id="test-project", prefix="ge-test")
    mock_sm_client.access_secret_version.side_effect = NotFound("gone")

    await storage.expire_user_token("tok-missing", time.time() + 60)

    assert not mock_sm_client.add_secret_version.called
