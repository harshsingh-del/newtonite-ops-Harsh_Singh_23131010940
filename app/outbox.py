from typing import Callable

from psycopg.types.json import Jsonb

from .db import pool

Handler = Callable[[dict, object], None]  # (job, connection) -> None


def enqueue(conn, type_: str, payload: dict, dedupe_key: str) -> None:
    """Call inside the same transaction as the change that needs the side effect."""
    conn.execute(
        "INSERT INTO outbox (type, payload, dedupe_key) VALUES (%s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING",
        (type_, Jsonb(payload), dedupe_key),
    )


def notify_user(job: dict, conn) -> None:
    """Idempotent by construction: notifications.dedupe_key is UNIQUE, so running the same job
    twice (crash after the insert, duplicate delivery) still yields exactly one row."""
    p = job["payload"]
    conn.execute(
        "INSERT INTO notifications (user_id, item_id, message, dedupe_key) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (dedupe_key) DO NOTHING",
        (p["userId"], p.get("itemId"), p["message"], job["dedupe_key"]),
    )


DEFAULT_HANDLERS: dict[str, Handler] = {"notify_user": notify_user}
STUCK_AFTER_SECONDS = 60


def process_one(handlers: dict[str, Handler] | None = None, backoff_seconds: float = 2) -> bool:
    """Claim one runnable job (SKIP LOCKED: several workers never take the same one), run it, then mark it
    done / schedule a retry with exponential backoff / mark it failed after max_attempts.
    A job stuck in 'processing' (worker crashed) becomes runnable again after 60 s. False = nothing to do."""
    handlers = DEFAULT_HANDLERS if handlers is None else handlers

    with pool.connection() as conn:  # transaction 1: claim
        row = conn.execute(
            """SELECT id FROM outbox
               WHERE (status = 'pending' AND next_run_at <= now())
                  OR (status = 'processing' AND locked_at < now() - make_interval(secs => %s::float8))
               ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""",
            (STUCK_AFTER_SECONDS,),
        ).fetchone()
        job = None
        if row:
            job = conn.execute(
                """UPDATE outbox SET status = 'processing', locked_at = now(), attempts = attempts + 1
                   WHERE id = %s RETURNING id, type, payload, dedupe_key, attempts, max_attempts""",
                (row["id"],),
            ).fetchone()
    if job is None:
        return False

    try:
        handler = handlers.get(job["type"])
        if handler is None:
            raise RuntimeError(f'no handler for job type "{job["type"]}"')
        with pool.connection() as conn:  # transaction 2: the effect and "done" commit together
            handler(job, conn)
            conn.execute("UPDATE outbox SET status = 'done', locked_at = NULL, last_error = NULL WHERE id = %s", (job["id"],))
    except Exception as err:  # noqa: BLE001 - any handler failure is retried
        with pool.connection() as conn:
            if job["attempts"] >= job["max_attempts"]:
                conn.execute(
                    "UPDATE outbox SET status = 'failed', locked_at = NULL, last_error = %s WHERE id = %s",
                    (str(err), job["id"]),
                )
            else:
                delay = backoff_seconds * 2 ** (job["attempts"] - 1)
                conn.execute(
                    "UPDATE outbox SET status = 'pending', locked_at = NULL, last_error = %s, "
                    "next_run_at = now() + make_interval(secs => %s::float8) WHERE id = %s",
                    (str(err), delay, job["id"]),
                )
    return True
