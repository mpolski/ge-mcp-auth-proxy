"""PKCE (Proof Key for Code Exchange - RFC 7636) verification utilities."""

import base64
import hashlib
from typing import Optional


import secrets


def compute_s256_challenge(code_verifier: str) -> str:
    """Apply the RFC 7636 S256 transform: BASE64URL(SHA256(ASCII(verifier))).

    Shared by generation, verification and diagnostics so that all three agree by
    construction, including the unpadded base64url encoding RFC 7636 requires.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def generate_pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for RFC 7636 S256 PKCE."""
    code_verifier = secrets.token_urlsafe(64)
    return code_verifier, compute_s256_challenge(code_verifier)


def verify_code_challenge(
    code_verifier: Optional[str],
    code_challenge: Optional[str],
    method: Optional[str] = "S256",
) -> bool:
    """Verify code_verifier against stored code_challenge according to RFC 7636.
    
    If no code_challenge was stored during authorization, verification passes.
    """
    if not code_challenge:
        return True
    if not code_verifier:
        return False

    norm_method = (method or "S256").upper()
    if norm_method == "S256":
        computed = compute_s256_challenge(code_verifier)
        target = code_challenge.rstrip("=")
        return computed == target
    elif norm_method == "PLAIN":
        return code_verifier == code_challenge

    return False

