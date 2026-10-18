import json

from app.outbox import DEFAULT_HANDLERS, process_one

from .conftest import db, parallel


def drain(handlers=None):
    n = 0
    while process_one(handlers or DEFAULT_HANDLERS, backoff_seconds=0):
        n += 1
    return n


def count(table):
    return db(f"SELECT count(*)::int AS n FROM {table}")[0]["n"]


def test_job_is_written_with_the_assignment_and_the_worker_delivers_it(w):
    item = w.create_item("memA1")
    w.call("PATCH", f"/api/items/{item['id']}", "leadA", json={"version": 1, "assignee_id": w.ids["memA2"]})
    assert db("SELECT type, status FROM outbox") == [{"type": "notify_user", "status": "pending"}]

    drain()
    assert db("SELECT status FROM outbox")[0]["status"] == "done"
    inbox = w.call("GET", "/api/notifications", "memA2").json()["notifications"]
    assert len(inbox) == 1
    assert "You were assigned" in inbox[0]["message"]


def test_no_job_is_created_when_the_change_fails(w):
    db("TRUNCATE outbox, notifications")
    item = w.create_item("memA1")
    r = w.call("PATCH", f"/api/items/{item['id']}", "leadA", json={"version": 99, "assignee_id": w.ids["memA2"]})
    assert r.status_code == 409
    assert count("outbox") == 0


def test_processing_the_same_job_twice_creates_one_notification(w):
    db("TRUNCATE outbox, notifications")
    item = w.create_item("memA1")
    w.call("PATCH", f"/api/items/{item['id']}", "leadA", json={"version": 1, "assignee_id": w.ids["memA3"]})
    drain()
    # Simulate "worker crashed after the side effect but before marking done": the job runs again.
    db("UPDATE outbox SET status = 'pending'")
    drain()
    assert count("notifications") == 1
    assert db("SELECT attempts FROM outbox")[0]["attempts"] == 2


def test_failing_job_is_retried_up_to_max_attempts_then_marked_failed(w):
    db("TRUNCATE outbox, notifications")
    db("INSERT INTO outbox (type, payload, dedupe_key, max_attempts) VALUES ('flaky', '{}', 'flaky-1', 3)")
    calls = []

    def flaky(job, conn):
        calls.append(1)
        raise RuntimeError("smtp down")

    drain({"flaky": flaky})
    assert len(calls) == 3
    assert db("SELECT status, attempts, last_error FROM outbox")[0] == {"status": "failed", "attempts": 3, "last_error": "smtp down"}
    assert process_one({"flaky": flaky}, backoff_seconds=0) is False  # failed jobs are left alone


def test_job_that_fails_once_then_succeeds_ends_up_done(w):
    db("TRUNCATE outbox, notifications")
    db("INSERT INTO outbox (type, payload, dedupe_key) VALUES ('once_flaky', '{}', 'of-1')")
    calls = []

    def once_flaky(job, conn):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")

    drain({"once_flaky": once_flaky})
    assert db("SELECT status, attempts FROM outbox")[0] == {"status": "done", "attempts": 2}


def test_backoff_a_failed_job_is_not_runnable_until_next_run_at(w):
    db("TRUNCATE outbox")
    db("INSERT INTO outbox (type, payload, dedupe_key) VALUES ('boom', '{}', 'boom-1')")

    def boom(job, conn):
        raise RuntimeError("x")

    assert process_one({"boom": boom}, backoff_seconds=60) is True
    assert process_one({"boom": boom}, backoff_seconds=60) is False


def test_job_left_processing_by_a_crashed_worker_is_recovered(w):
    db("TRUNCATE outbox, notifications")
    db(
        "INSERT INTO outbox (type, payload, dedupe_key, status, locked_at, attempts) "
        "VALUES ('notify_user', %s, 'stuck-1', 'processing', now() - interval '5 minutes', 1)",
        (json.dumps({"userId": w.ids["memA1"], "itemId": None, "message": "recovered"}),),
    )
    drain()
    assert db("SELECT status FROM outbox")[0]["status"] == "done"
    assert db("SELECT message FROM notifications")[0]["message"] == "recovered"


def test_three_workers_at_once_never_run_the_same_job_twice(w):
    db("TRUNCATE outbox, notifications")
    for i in range(10):
        db("INSERT INTO outbox (type, payload, dedupe_key) VALUES ('count', '{}', %s)", (f"c-{i}",))
    handled = []

    import time

    def count_job(job, conn):
        handled.append(job["id"])
        time.sleep(0.01)

    parallel(lambda: drain({"count": count_job}), lambda: drain({"count": count_job}), lambda: drain({"count": count_job}))
    assert len(handled) == 10 and len(set(handled)) == 10
    assert db("SELECT count(*)::int AS n FROM outbox WHERE status = 'done'")[0]["n"] == 10
