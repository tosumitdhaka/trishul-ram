"""Auth router: login / logout / me endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tram.api.auth import create_token, extract_bearer, parse_users, verify_password, verify_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    config = request.app.state.config
    if not config.auth_users:
        raise HTTPException(403, "User auth not configured (TRAM_AUTH_USERS not set)")
    users = parse_users(config.auth_users)
    if not verify_password(body.username, body.password, users):
        raise HTTPException(401, "Invalid credentials")
    token = create_token(body.username)
    return {"token": token, "username": body.username}


@router.get("/me")
async def me(request: Request):
    token = extract_bearer(request)
    username = verify_token(token) if token else None
    if not username:
        raise HTTPException(401, "Not authenticated")
    return {"username": username}
