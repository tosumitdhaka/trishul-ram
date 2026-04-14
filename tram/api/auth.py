"""HMAC-based session tokens for browser UI authentication.

Tokens are signed with a per-process secret and expire after TTL seconds.
All tokens are invalidated on daemon restart (stateless — no token store needed).
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
# Password hashing — scrypt (OWASP recommended)
# ---------------------------------------------------------------------------
# Parameters: N=2^14 (16 384), r=8, p=1  → ~64 MB RAM, ~100 ms on modern HW.
# Stored format: "scrypt$<hex-salt>$<hex-digest>"
# Legacy SHA-256 hashes ("sha256$...") are still verifiable for migration.
# ---------------------------------------------------------------------------

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Return a salted scrypt hash string: ``scrypt$<salt>$<hex>``."""
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt.encode(),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    ).hex()
    return f"scrypt${salt}${digest}"


def verify_hashed_password(password: str, stored_hash: str) -> bool:
    """Verify *password* against a hash produced by :func:`hash_password`.

    Supports both the current ``scrypt`` scheme and the legacy ``sha256``
    scheme so that existing stored hashes remain valid until rehashed.
    """
    try:
        scheme, salt, digest = stored_hash.split("$", 2)
    except Exception:
        return False

    if scheme == "scrypt":
        try:
            candidate = hashlib.scrypt(
                password.encode(),
                salt=salt.encode(),
                n=_SCRYPT_N,
                r=_SCRYPT_R,
                p=_SCRYPT_P,
                dklen=_SCRYPT_DKLEN,
            ).hex()
            return hmac.compare_digest(candidate, digest)
        except Exception:
            return False

    if scheme == "sha256":
        # Legacy path — still verifiable; rehash on next login recommended.
        try:
            expected = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
            return hmac.compare_digest(expected, digest)
        except Exception:
            return False

    return False


# ---------------------------------------------------------------------------
# Signing secret — shared across cluster nodes via TRAM_AUTH_SECRET env var.
# Falls back to a per-process random (tokens invalidated on restart) when unset.
# ---------------------------------------------------------------------------

_SECRET = _os.environ.get("TRAM_AUTH_SECRET") or secrets.token_hex(32)


def parse_users(raw: str) -> dict[str, str]:
    """Parse TRAM_AUTH_USERS value: ``user1:pass1,user2:pass2``.

    Values that are already hashed (``scrypt$...`` or ``sha256$...``) are
    stored as-is.  Plain-text values are hashed with scrypt on first parse so
    that the plaintext is never kept in memory beyond this call.
    """
    users: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            u, p = entry.split(":", 1)
            u, p = u.strip(), p.strip()
            if u:
                # Hash plain-text passwords immediately so they are never
                # compared in plaintext again.  Pre-hashed values pass through.
                if not (p.startswith("scrypt$") or p.startswith("sha256$")):
                    p = hash_password(p)
                users[u] = p
    return users


def verify_password(username: str, password: str, users: dict[str, str]) -> bool:
    """Verify *password* for *username* against the users dict.

    All passwords stored in *users* are hashed (either scrypt or legacy sha256)
    because :func:`parse_users` hashes plain-text values on load.
    """
    stored = users.get(username)
    if stored is None:
        return False
    return verify_hashed_password(password, stored)


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
