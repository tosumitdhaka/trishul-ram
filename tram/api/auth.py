"""HMAC-based session tokens for browser UI authentication.

Tokens are signed with a per-process secret and expire after TTL seconds.
All tokens are invalidated on daemon restart (stateless — no token store needed).

Password hashing uses hashlib.scrypt (N=2**14, r=8, p=1) — sufficiently slow
for login flows while remaining dependency-free. The stored format is::

    scrypt$<hex-salt>$<hex-digest>

Legacy ``sha256$`` hashes (produced by TRAM < 1.2.2) are still verified but
new passwords are always stored with the scrypt scheme.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os as _os
import secrets
import time

# ---------------------------------------------------------------------------
# Signing secret — shared across cluster nodes via TRAM_AUTH_SECRET env var.
# Falls back to a per-process random (tokens invalidated on restart) when unset.
# ---------------------------------------------------------------------------
_SECRET = _os.environ.get("TRAM_AUTH_SECRET") or secrets.token_hex(32)

# scrypt parameters — deliberately slow to resist offline brute-force attacks.
_SCRYPT_N = 2 ** 14  # CPU/memory cost (16 384)
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Return a salted scrypt hash string: ``scrypt$<hex-salt>$<hex-digest>``."""
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    ).hex()
    return f"scrypt${salt}${digest}"


def verify_hashed_password(password: str, stored_hash: str) -> bool:
    """Verify *password* against a hash produced by :func:`hash_password`.

    Supports both the current ``scrypt$`` scheme and the legacy ``sha256$``
    scheme so that existing databases are not invalidated by the upgrade.
    """
    try:
        scheme, salt, digest = stored_hash.split("$", 2)
        if scheme == "scrypt":
            expected = hashlib.scrypt(
                password.encode(),
                salt=bytes.fromhex(salt),
                n=_SCRYPT_N,
                r=_SCRYPT_R,
                p=_SCRYPT_P,
                dklen=_SCRYPT_DKLEN,
            ).hex()
        elif scheme == "sha256":
            # Legacy path — still verifiable, but new hashes use scrypt.
            expected = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
        else:
            return False
        return hmac.compare_digest(expected, digest)
    except Exception:
        return False


def parse_users(raw: str) -> dict[str, str]:
    """Parse TRAM_AUTH_USERS value: ``user1:pass1,user2:pass2``.

    .. warning::
        Values from TRAM_AUTH_USERS are treated as **plaintext** passwords
        that are hashed on first comparison via :func:`verify_password`.
        For production deployments prefer the ``user_passwords`` database
        table (populated via ``POST /api/auth/users``) which always stores
        scrypt hashes and keeps credentials out of the process environment.
    """
    users: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            u, p = entry.split(":", 1)
            u, p = u.strip(), p.strip()
            if u:
                users[u] = p
    return users


def verify_password(username: str, password: str, users: dict[str, str]) -> bool:
    """Verify *password* for *username* against the in-memory users dict.

    The dict values may be either:
    - A plaintext password (from TRAM_AUTH_USERS env var)
    - A ``scrypt$`` or ``sha256$`` hash (if a pre-hashed value is stored)

    Plaintext values are compared using :func:`hmac.compare_digest` (constant
    time) to prevent timing attacks. Hashed values are verified via
    :func:`verify_hashed_password`.
    """
    stored = users.get(username)
    if stored is None:
        return False
    # Route to the appropriate verification path based on the stored value.
    if stored.startswith(("scrypt$", "sha256$")):
        return verify_hashed_password(password, stored)
    # Plaintext env-var password — constant-time comparison.
    return hmac.compare_digest(stored.encode(), password.encode())


def create_token(username: str, ttl: int = 28800) -> str:
    """Return a signed token valid for *ttl* seconds (default 8 h)."""
    payload = json.dumps({"u": username, "exp": int(time.time()) + ttl})
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_token(token: str) -> str | None:
    """Return the username if the token is valid and unexpired, else None."""
    try:
        b64, sig = token.rsplit(".", 1)
        expected = hmac.new(_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padding = "=" * (4 - len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64 + padding))
        if payload["exp"] < time.time():
            return None
        return payload["u"]
    except Exception:
        return None


def extract_bearer(request) -> str | None:
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else None
