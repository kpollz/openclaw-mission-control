"""Password hashing and verification for password-based authentication.

Uses the same PBKDF2-HMAC-SHA256 infrastructure already established for agent
tokens (agent_tokens.py). This avoids adding a new dependency (bcrypt/passlib)
while maintaining strong security: 200 000 iterations + per-user random salt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Reuse the same iteration count as agent tokens — proven secure for this app.
_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a plaintext password and return a storable string.

    Format: ``base64(salt):base64(digest)``
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return f"{base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()}"


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored hash.

    Returns ``True`` when the password matches. Uses constant-time comparison
    to mitigate timing attacks.
    """
    if ":" not in stored_hash:
        return False
    salt_b64, digest_b64 = stored_hash.split(":", 1)
    try:
        salt = base64.b64decode(salt_b64)
        stored_digest = base64.b64decode(digest_b64)
    except Exception:
        return False
    candidate_digest = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return hmac.compare_digest(candidate_digest, stored_digest)
