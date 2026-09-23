"""RFC 7591 Dynamic Client Registration (DCR) helper for upstream MCP OAuth services.

Many desktop-first MCP services do not have an IT
admin console to manually pre-register static OAuth clients and redirect URIs. Instead,
they implement RFC 7591 Dynamic Client Registration.

This helper allows registering a public PKCE client or confidential client with any
RFC 7591-compliant provider.
"""

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger(__name__)


class DCRError(RuntimeError):
    """Raised when RFC 7591 Dynamic Client Registration fails."""
    pass


def register_client(
    registration_url: str,
    redirect_uris: List[str],
    client_name: str = "Gemini Enterprise MCP Broker",
    token_endpoint_auth_method: str = "none",
    grant_types: Optional[List[str]] = None,
    response_types: Optional[List[str]] = None,
    scope: Optional[str] = None,
    initial_access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute RFC 7591 Dynamic Client Registration request against upstream provider (sync)."""
    payload: Dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": token_endpoint_auth_method,
        "grant_types": grant_types or ["authorization_code", "refresh_token"],
        "response_types": response_types or ["code"],
    }
    if scope:
        payload["scope"] = scope

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if initial_access_token:
        headers["Authorization"] = f"Bearer {initial_access_token}"

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(registration_url, json=payload, headers=headers)

    if resp.status_code not in (200, 201):
        raise DCRError(
            f"Dynamic Client Registration failed ({resp.status_code}): {resp.text}"
        )

    return resp.json()


async def async_register_client(
    registration_url: str,
    redirect_uris: List[str],
    client_name: str = "Gemini Enterprise MCP Broker",
    token_endpoint_auth_method: str = "none",
    grant_types: Optional[List[str]] = None,
    response_types: Optional[List[str]] = None,
    scope: Optional[str] = None,
    initial_access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute RFC 7591 Dynamic Client Registration request against upstream provider (async)."""
    payload: Dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": token_endpoint_auth_method,
        "grant_types": grant_types or ["authorization_code", "refresh_token"],
        "response_types": response_types or ["code"],
    }
    if scope:
        payload["scope"] = scope

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if initial_access_token:
        headers["Authorization"] = f"Bearer {initial_access_token}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(registration_url, json=payload, headers=headers)

    if resp.status_code not in (200, 201):
        raise DCRError(
            f"Dynamic Client Registration failed ({resp.status_code}): {resp.text}"
        )

    return resp.json()


def main():
    parser = argparse.ArgumentParser(
        description="Register an OAuth client with an RFC 7591 Dynamic Client Registration endpoint."
    )
    parser.add_argument(
        "--registration-url",
        required=True,
        help="Upstream RFC 7591 registration URL, from the provider's discovery document",
    )
    parser.add_argument(
        "--redirect-uri",
        action="append",
        required=True,
        help="Allowed redirect URI(s), e.g. https://<cloud-run-domain>/oauth/callback",
    )
    parser.add_argument(
        "--client-name",
        default="Gemini Enterprise MCP Broker",
        help="Client name displayed on provider consent screens",
    )
    parser.add_argument(
        "--auth-method",
        default="none",
        choices=["none", "client_secret_post", "client_secret_basic"],
        help="Token endpoint auth method (default: none for public PKCE)",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="Requested OAuth scopes",
    )
    parser.add_argument(
        "--initial-token",
        default=None,
        help="Initial registration bearer token (if required by provider)",
    )

    args = parser.parse_args()

    try:
        result = register_client(
            registration_url=args.registration_url,
            redirect_uris=args.redirect_uri,
            client_name=args.client_name,
            token_endpoint_auth_method=args.auth_method,
            scope=args.scope,
            initial_access_token=args.initial_token,
        )
        print(json.dumps(result, indent=2))
        client_id = result.get("client_id")
        if client_id:
            print(f"\n[SUCCESS] Registered client_id: {client_id}", file=sys.stderr)
            print(f"Set UPSTREAM_CLIENT_ID={client_id}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
