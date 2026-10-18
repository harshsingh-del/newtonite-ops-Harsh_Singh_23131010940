import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import bcrypt
import psycopg
import pytest

# Must be set BEFORE the app is imported (it reads these at import time).
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgres://postgres:postgres@localhost:5432/ops_test")
os.environ["DATABASE_URL"] = TEST_URL
os.environ["JWT_SECRET"] = "test-secret"


def _ensure_database() -> None:
    """Create the test database if it does not exist yet (so `pytest` works on a fresh machine)."""
    name = re.search(r"/([A-Za-z0-9_]+)$", TEST_URL).group(1)
    admin_url = TEST_URL[: TEST_URL.rindex("/")] + "/postgres"
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                conn.execute(f"CREATE DATABASE {name}")
    except psycopg.OperationalError as e:
        pytest.exit(f"Could not reach Postgres to prepare the test database. Is it running? (docker compose up -d db)\n{e}")


_ensure_database()

from fastapi.testclient import TestClient  # noqa: E402

from app.db import migrate, pool  # noqa: E402
from app.main import app  # noqa: E402


@dataclass
class World:
    team_a: str
    team_b: str
    tokens: dict
    ids: dict

    def call(self, method: str, path: str, user: str | None = None, key: str | None = None, **kw):
        """One HTTP call against the real app. Each call uses its own client so calls can run in parallel threads."""
        headers = {}
        if user:
            headers["Authorization"] = f"Bearer {self.tokens[user]}"
        if key:
            headers["Idempotency-Key"] = key
        return TestClient(app).request(method, path, headers=headers, **kw)

    def create_item(self, as_user: str, **over):
        body = {"title": "Payment stuck", "type": "payment", "priority": "high", "team_id": self.team_a, **over}
        r = self.call("POST", "/api/items", as_user, json=body)
        assert r.status_code == 201, r.text
        return r.json()


def parallel(*fns):
    """Run callables at the same time in separate threads and return their results in order."""
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        return [f.result() for f in [ex.submit(fn) for fn in fns]]


def db(sql: str, params=None):
    with pool.connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def w() -> World:
    """Wipe the test DB and create a small, known set of users (rebuilt for each test module)."""
    migrate()
    # activity_events is cleared by the cascade; its trigger only blocks row-level UPDATE/DELETE.
    db("TRUNCATE notifications, outbox, idempotency_keys, memberships, work_items, teams, users RESTART IDENTITY CASCADE")
    pw = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode()
    team_a = db("INSERT INTO teams (name) VALUES ('Team A') RETURNING id")[0]["id"]
    team_b = db("INSERT INTO teams (name) VALUES ('Team B') RETURNING id")[0]["id"]
    spec = [
        ("leadA", "Lead A", team_a, "lead"),
        ("memA1", "Member A1", team_a, "member"),
        ("memA2", "Member A2", team_a, "member"),
        ("memA3", "Member A3", team_a, "member"),
        ("viewA", "Viewer A", team_a, "viewer"),
        ("outsider", "Outsider", team_b, "member"),
    ]
    ids, tokens = {}, {}
    for key, name, team, role in spec:
        uid = db(
            "INSERT INTO users (email, name, password_hash) VALUES (%s,%s,%s) RETURNING id",
            (f"{key.lower()}@t.test", name, pw),
        )[0]["id"]
        ids[key] = uid
        db("INSERT INTO memberships (user_id, team_id, role) VALUES (%s,%s,%s)", (uid, team, role))
        r = TestClient(app).post("/auth/login", json={"email": f"{key.lower()}@t.test", "password": "pw"})
        tokens[key] = r.json()["token"]
    return World(team_a=team_a, team_b=team_b, tokens=tokens, ids=ids)


def pytest_sessionfinish(session, exitstatus):
    pool.close()
