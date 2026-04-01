"""HMAC-based session tokens for browser UI authentication.

Tokens are signed with a per-process secret and expire after TTL seconds.
All tokens are invalidated on daemon restart (stateless — no token store needed).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


def hash_password(password: str) -> str:
    """Return a salted SHA-256 hash string: ``sha256$<salt>$<hex>``."""
    salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"sha256${salt}${digest}"


def verify_hashed_password(password: str, stored_hash: str) -> bool:
    """Verify *password* against a hash produced by :func:`hash_password`."""
    try:
        scheme, salt, digest = stored_hash.split("$", 2)
        if scheme != "sha256":
            return False
        expected = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
        return hmac.compare_digest(expected, digest)
    except Exception:
        return False

# Signing secret — shared across cluster nodes via TRAM_AUTH_SECRET env var.
# Falls back to a per-process random (tokens invalidated on restart) when unset.
import os as _os  # noqa: E402

_SECRET = _os.environ.get("TRAM_AUTH_SECRET") or secrets.token_hex(32)


def parse_users(raw: str) -> dict[str, str]:
    """Parse TRAM_AUTH_USERS value: ``user1:pass1,user2:pass2``."""
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
    stored = users.get(username)
    if stored is None:
        return False
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
