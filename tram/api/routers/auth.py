"""Auth router: login / logout / me / change-password endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tram.api.auth import (
    create_token,
    extract_bearer,
    hash_password,
    parse_users,
    verify_hashed_password,
    verify_password,
    verify_token,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _resolve_password(username: str, password: str, config, db) -> bool:
    """Verify password: DB hash overrides env-var credentials."""
    if db is not None:
        stored_hash = db.get_password_hash(username)
        if stored_hash is not None:
            return verify_hashed_password(password, stored_hash)
    if not config.auth_users:
        return False
    return verify_password(username, password, parse_users(config.auth_users))


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    config = request.app.state.config
    db = getattr(request.app.state, "db", None)
    if not config.auth_users and db is None:
        raise HTTPException(403, "User auth not configured (TRAM_AUTH_USERS not set)")
    if not config.auth_users:
        raise HTTPException(403, "User auth not configured (TRAM_AUTH_USERS not set)")
    if not _resolve_password(body.username, body.password, config, db):
        raise HTTPException(401, "Invalid credentials")
    token = create_token(body.username)
    return {"token": token, "username": body.username}


@router.get("/me")
async def me(request: Request):
    config = request.app.state.config
    if not config.auth_users:
        raise HTTPException(403, "User auth not configured (TRAM_AUTH_USERS not set)")
    token = extract_bearer(request)
    username = verify_token(token) if token else None
    if not username:
        raise HTTPException(401, "Not authenticated")
    return {"username": username}


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, request: Request):
    config = request.app.state.config
    db = getattr(request.app.state, "db", None)
    if not config.auth_users:
        raise HTTPException(403, "User auth not configured")
    # Require a valid session token
    token = extract_bearer(request)
    username = verify_token(token) if token else None
    if not username:
        raise HTTPException(401, "Not authenticated")
    # Verify current password
    if not _resolve_password(username, body.current_password, config, db):
        raise HTTPException(401, "Current password is incorrect")
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    if db is None:
        raise HTTPException(503, "Password change requires a database (TRAM_DB_URL)")
    db.set_password_hash(username, hash_password(body.new_password))
    return {"ok": True, "username": username}
