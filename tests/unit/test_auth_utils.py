"""Tests for tram.api.auth — token creation/verification and password utilities."""
from __future__ import annotations

from unittest.mock import MagicMock

from tram.api.auth import (
    create_token,
    extract_bearer,
    hash_password,
    parse_users,
    verify_hashed_password,
    verify_password,
    verify_token,
)


class TestHashPassword:
    def test_returns_scrypt_prefix(self):
        h = hash_password("secret")
        assert h.startswith("scrypt$")

    def test_three_parts(self):
        h = hash_password("secret")
        parts = h.split("$")
        assert len(parts) == 3

    def test_unique_salt(self):
        h1 = hash_password("secret")
        h2 = hash_password("secret")
        assert h1 != h2  # different salts

    def test_verify_correct_password(self):
        h = hash_password("mysecret")
        assert verify_hashed_password("mysecret", h) is True

    def test_verify_wrong_password(self):
        h = hash_password("mysecret")
        assert verify_hashed_password("wrong", h) is False

    def test_verify_malformed_hash(self):
        assert verify_hashed_password("pw", "notahash") is False

    def test_verify_wrong_scheme(self):
        assert verify_hashed_password("pw", "md5$salt$digest") is False


class TestParseUsers:
    def test_single_user(self):
        users = parse_users("admin:pass")
        assert users == {"admin": "pass"}

    def test_multiple_users(self):
        users = parse_users("admin:pass,user2:secret")
        assert users == {"admin": "pass", "user2": "secret"}

    def test_password_with_colon(self):
        users = parse_users("admin:p:w")
        assert users["admin"] == "p:w"

    def test_empty_string(self):
        assert parse_users("") == {}

    def test_whitespace_stripped(self):
        users = parse_users(" admin : pass ")
        assert "admin" in users

    def test_skips_entry_without_colon(self):
        users = parse_users("nocoion,user:pass")
        assert "nocoion" not in users
        assert "user" in users


class TestVerifyPassword:
    def test_correct_credentials(self):
        users = {"admin": "pass123"}
        assert verify_password("admin", "pass123", users) is True

    def test_wrong_password(self):
        users = {"admin": "pass123"}
        assert verify_password("admin", "wrong", users) is False

    def test_unknown_user(self):
        users = {"admin": "pass123"}
        assert verify_password("unknown", "pass123", users) is False


class TestCreateAndVerifyToken:
    def test_valid_token_returns_username(self):
        token = create_token("alice")
        assert verify_token(token) == "alice"

    def test_tampered_token_rejected(self):
        token = create_token("alice")
        # Tamper the payload
        tampered = token[:-5] + "XXXXX"
        assert verify_token(tampered) is None

    def test_expired_token_rejected(self):
        # TTL of 0 means expired immediately
        token = create_token("alice", ttl=-1)
        assert verify_token(token) is None

    def test_garbage_token_rejected(self):
        assert verify_token("notavalidtoken") is None

    def test_empty_string_rejected(self):
        assert verify_token("") is None


class TestExtractBearer:
    def test_valid_bearer_header(self):
        req = MagicMock()
        req.headers = {"Authorization": "Bearer mytoken123"}
        assert extract_bearer(req) == "mytoken123"

    def test_missing_header(self):
        req = MagicMock()
        req.headers = {}
        assert extract_bearer(req) is None

    def test_non_bearer_scheme(self):
        req = MagicMock()
        req.headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        assert extract_bearer(req) is None
