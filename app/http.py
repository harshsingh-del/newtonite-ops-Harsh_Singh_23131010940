import hashlib
import json
from typing import Callable

import jwt
from fastapi import Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from . import config
from .authz import AuthUser, load_user
from .db import pool
from .errors import HttpError


def get_user(authorization: str | None = Header(default=None)) -> AuthUser:
    """Auth dependency. Roles are re-read from the DB on every request, so a demoted user loses access immediately."""
    try:
        if not authorization or not authorization.startswith("Bearer "):
            raise ValueError
        payload = jwt.decode(authorization[7:], config.JWT_SECRET, algorithms=["HS256"])
        with pool.connection() as conn:
            user = load_user(conn, payload["sub"])
        if user is None:
            raise ValueError
        return user
    except Exception:
        raise HttpError(401, "Please sign in")


def run(request: Request, user: AuthUser, handler: Callable, body=None) -> JSONResponse:
    """Run `handler(conn) -> (status, body)` in ONE database transaction.

    If the client sent an Idempotency-Key on a write, the key is claimed inside the same transaction:
      * first request: handler runs, response is stored with the key, both commit together
      * repeat (retry / double-click): the stored response is replayed and nothing else happens
      * concurrent duplicate: its INSERT blocks on the unique index until the first commits, then replays
      * same key, different request: 422
    A failed request rolls back (including the key), so retrying after fixing the input is safe.
    """
    key = None if request.method == "GET" else request.headers.get("idempotency-key") or None
    headers: dict[str, str] = {}
    with pool.connection() as conn:
        if not key:
            status, payload = handler(conn)
        else:
            if len(key) > 200:
                raise HttpError(400, "Idempotency-Key too long")
            raw = json.dumps(body.model_dump(mode="json", exclude_unset=True), sort_keys=True) if body is not None else ""
            req_hash = hashlib.sha256(f"{request.method} {request.url.path} {raw}".encode()).hexdigest()
            inserted = conn.execute(
                "INSERT INTO idempotency_keys (user_id, key, request_hash) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, key) DO NOTHING RETURNING key",
                (user.id, key, req_hash),
            ).fetchone()
            if not inserted:
                prev = conn.execute(
                    "SELECT request_hash, status_code, response FROM idempotency_keys WHERE user_id = %s AND key = %s",
                    (user.id, key),
                ).fetchone()
                if prev["request_hash"] != req_hash:
                    raise HttpError(422, "This Idempotency-Key was already used for a different request")
                status, payload = prev["status_code"], prev["response"]
                headers["Idempotent-Replayed"] = "true"
            else:
                status, payload = handler(conn)
                conn.execute(
                    "UPDATE idempotency_keys SET status_code = %s, response = %s WHERE user_id = %s AND key = %s",
                    (status, Jsonb(jsonable_encoder(payload)), user.id, key),
                )
    return JSONResponse(content=jsonable_encoder(payload), status_code=status, headers=headers)
