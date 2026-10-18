import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .authz import AuthUser, check_team
from .db import pool
from .errors import HttpError, forbidden, not_found
from .http import get_user
from .items import router as items_router

app = FastAPI(title="Ops Desk", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------- error mapping: every error is {"error": "..."} ----------
@app.exception_handler(HttpError)
async def http_error(_: Request, err: HttpError):
    return JSONResponse(jsonable_encoder({"error": err.message, **err.extra}), status_code=err.status)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, err: RequestValidationError):
    issues = [{"path": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in err.errors()]
    return JSONResponse({"error": "Invalid request", "issues": issues}, status_code=400)


@app.get("/health")
def health():
    return {"ok": True}


# ---------- auth: seeded users, email + password -> signed token ----------
class LoginBody(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=1)


@app.post("/auth/login")
def login(body: LoginBody):
    with pool.connection() as conn:
        row = conn.execute("SELECT id, password_hash FROM users WHERE email = %s", (body.email.lower(),)).fetchone()
    ok = bool(row) and bcrypt.checkpw(body.password.encode(), row["password_hash"].encode())
    if not ok:
        return JSONResponse({"error": "Wrong email or password"}, status_code=401)
    token = jwt.encode(
        {"sub": row["id"], "exp": datetime.now(timezone.utc) + timedelta(hours=12)},
        config.JWT_SECRET,
        algorithm="HS256",
    )
    return {"token": token}


# ---------- small read endpoints ----------
@app.get("/api/me")
def me(user: AuthUser = Depends(get_user)):
    with pool.connection() as conn:
        teams = conn.execute(
            "SELECT t.id, t.name, m.role FROM memberships m JOIN teams t ON t.id = m.team_id "
            "WHERE m.user_id = %s ORDER BY t.name",
            (user.id,),
        ).fetchall()
    return {"user": {"id": user.id, "name": user.name, "email": user.email}, "teams": teams}


@app.get("/api/teams/{team_id}/members")
def team_members(team_id: str, user: AuthUser = Depends(get_user)):
    if not re.match(r"^[0-9a-fA-F-]{36}$", team_id):
        raise not_found("Team not found")
    perm = check_team(user, "read", team_id)
    if perm == "none":
        raise not_found("Team not found")
    if perm == "denied":
        raise forbidden()
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT u.id, u.name, m.role FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.team_id = %s ORDER BY u.name",
            (team_id,),
        ).fetchall()
    return {"members": rows}


@app.get("/api/notifications")
def notifications(user: AuthUser = Depends(get_user)):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, item_id, message, created_at FROM notifications WHERE user_id = %s ORDER BY id DESC LIMIT 50",
            (user.id,),
        ).fetchall()
    return {"notifications": rows}


app.include_router(items_router)


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(rest: str):
    return JSONResponse({"error": "Not found"}, status_code=404)


# Plain static UI (HTML/CSS/JS), no build step. Mounted last so API routes win.
app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "public", html=True), name="ui")
