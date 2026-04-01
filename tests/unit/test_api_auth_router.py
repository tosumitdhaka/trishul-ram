"""Tests for the auth router: login, me, change-password."""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.auth import create_token
from tram.api.routers.auth import router


def _make_app(auth_users="admin:pass123", db=None):
    app = FastAPI()
    app.include_router(router)

    mock_config = MagicMock()
    mock_config.auth_users = auth_users

    app.state.config = mock_config
    app.state.db = db
    return app


class TestLogin:
    def test_valid_credentials_returns_token(self):
        app = _make_app("admin:pass123")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "pass123"})
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["username"] == "admin"

    def test_wrong_password_returns_401(self):
        app = _make_app("admin:pass123")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

    def test_unknown_user_returns_401(self):
        app = _make_app("admin:pass123")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/auth/login", json={"username": "ghost", "password": "pass123"})
        assert resp.status_code == 401

    def test_no_auth_configured_returns_403(self):
        app = _make_app(auth_users=None)
        app.state.config.auth_users = None
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "pass"})
        assert resp.status_code == 403

    def test_db_password_hash_takes_priority(self):
        from tram.api.auth import hash_password
        hashed = hash_password("dbpass")
        mock_db = MagicMock()
        mock_db.get_password_hash.return_value = hashed

        app = _make_app("admin:envpass", db=mock_db)
        client = TestClient(app, raise_server_exceptions=False)
        # DB hash matches "dbpass", not the env "envpass"
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "dbpass"})
        assert resp.status_code == 200


class TestMe:
    def test_valid_token_returns_username(self):
        app = _make_app("admin:pass123")
        token = create_token("admin")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"

    def test_no_token_returns_401(self):
        app = _make_app("admin:pass123")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self):
        app = _make_app("admin:pass123")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer badtoken"})
        assert resp.status_code == 401

    def test_no_auth_configured_returns_403(self):
        app = _make_app(auth_users=None)
        app.state.config.auth_users = None
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/auth/me")
        assert resp.status_code == 403


class TestChangePassword:
    def _authed_client(self, db=None):
        mock_db = db or MagicMock()
        mock_db.get_password_hash.return_value = None  # use env credentials

        app = _make_app("admin:pass123", db=mock_db)
        token = create_token("admin")
        client = TestClient(app, raise_server_exceptions=False)
        return client, token

    def test_successful_change(self):
        client, token = self._authed_client()
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "pass123", "new_password": "newpass99"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_wrong_current_password_returns_401(self):
        client, token = self._authed_client()
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "wrongpass", "new_password": "newpass99"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    def test_short_new_password_returns_400(self):
        client, token = self._authed_client()
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "pass123", "new_password": "abc"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_no_token_returns_401(self):
        mock_db = MagicMock()
        mock_db.get_password_hash.return_value = None
        app = _make_app("admin:pass123", db=mock_db)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "pass123", "new_password": "newpass99"},
        )
        assert resp.status_code == 401

    def test_no_db_returns_503(self):
        app = _make_app("admin:pass123", db=None)
        token = create_token("admin")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": "pass123", "new_password": "newpass99"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
