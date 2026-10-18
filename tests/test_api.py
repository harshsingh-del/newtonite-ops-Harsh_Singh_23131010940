import random

import pytest

from .conftest import db, parallel


def events_for(item_id):
    return db("SELECT kind, data, actor_id FROM activity_events WHERE item_id = %s ORDER BY id", (item_id,))


class TestConcurrentClaim:
    def test_exactly_one_of_five_simultaneous_claims_wins(self, w):
        item = w.create_item("memA1")
        users = ["memA1", "memA2", "memA3", "leadA", "memA2"]
        results = parallel(*[lambda u=u: w.call("POST", f"/api/items/{item['id']}/claim", u) for u in users])
        assert sorted(r.status_code for r in results) == [200, 409, 409, 409, 409]

        row = db("SELECT assignee_id, version FROM work_items WHERE id = %s", (item["id"],))[0]
        winner = next(r for r in results if r.status_code == 200)
        assert row["assignee_id"] == winner.json()["assignee_id"]
        assert row["version"] == 2  # exactly one successful change
        claims = [e for e in events_for(item["id"]) if e["kind"] == "assignee_changed"]
        assert len(claims) == 1  # exactly one history entry
        loser = next(r for r in results if r.status_code == 409)
        assert "Already claimed" in loser.json()["error"]


class TestStaleEdits:
    def test_old_version_is_rejected_and_data_unchanged(self, w):
        item = w.create_item("memA1", title="Original")
        first = w.call("PATCH", f"/api/items/{item['id']}", "memA1", json={"version": item["version"], "title": "Edited by A1"})
        assert first.status_code == 200
        assert first.json()["version"] == item["version"] + 1

        stale = w.call("PATCH", f"/api/items/{item['id']}", "leadA", json={"version": item["version"], "title": "Edited by lead (stale)"})
        assert stale.status_code == 409
        assert stale.json()["current"]["title"] == "Edited by A1"  # the 409 carries the latest item
        assert stale.json()["current"]["version"] == item["version"] + 1

        row = db("SELECT title, version FROM work_items WHERE id = %s", (item["id"],))[0]
        assert row == {"title": "Edited by A1", "version": item["version"] + 1}

    def test_two_simultaneous_edits_one_wins_one_conflicts(self, w):
        item = w.create_item("memA1")
        rs = parallel(
            lambda: w.call("PATCH", f"/api/items/{item['id']}", "memA1", json={"version": 1, "priority": "urgent"}),
            lambda: w.call("PATCH", f"/api/items/{item['id']}", "leadA", json={"version": 1, "priority": "low"}),
        )
        assert sorted(r.status_code for r in rs) == [200, 409]


class TestIdempotency:
    def body(self, w, title="Double click"):
        return {"title": title, "type": "task", "priority": "low", "team_id": w.team_a}

    def test_same_key_twice_creates_one_item_sequentially_and_concurrently(self, w):
        key = f"key-{random.random()}"
        first = w.call("POST", "/api/items", "memA1", key=key, json=self.body(w))
        second = w.call("POST", "/api/items", "memA1", key=key, json=self.body(w))
        assert first.status_code == 201 and second.status_code == 201
        assert second.json()["id"] == first.json()["id"]
        assert second.headers["idempotent-replayed"] == "true"

        key2 = f"key-{random.random()}"
        race = parallel(*[lambda: w.call("POST", "/api/items", "memA1", key=key2, json=self.body(w, "Race")) for _ in range(3)])
        assert len({r.json()["id"] for r in race}) == 1

        n = db("SELECT count(*)::int AS n FROM work_items WHERE title IN ('Double click','Race')")[0]["n"]
        assert n == 2

    def test_reused_key_with_different_body_is_422_and_keys_are_per_user(self, w):
        key = f"reuse-{random.random()}"
        w.call("POST", "/api/items", "memA1", key=key, json=self.body(w, "One"))
        diff = w.call("POST", "/api/items", "memA1", key=key, json=self.body(w, "Two"))
        assert diff.status_code == 422
        other = w.call("POST", "/api/items", "memA2", key=key, json=self.body(w, "Two"))
        assert other.status_code == 201

    def test_failed_requests_are_not_stored_so_a_retry_can_succeed(self, w):
        key = f"fail-{random.random()}"
        bad = w.call("POST", "/api/items", "memA1", key=key, json=self.body(w, ""))
        assert bad.status_code == 400
        ok = w.call("POST", "/api/items", "memA1", key=key, json=self.body(w, "Fixed"))
        assert ok.status_code == 201

    def test_works_on_other_writes_too(self, w):
        item = w.create_item("memA1")
        key = f"claim-{random.random()}"
        a = w.call("POST", f"/api/items/{item['id']}/claim", "memA1", key=key)
        b = w.call("POST", f"/api/items/{item['id']}/claim", "memA1", key=key)
        assert (a.status_code, b.status_code) == (200, 200)  # the retry is replayed, not a 409
        assert db("SELECT version FROM work_items WHERE id = %s", (item["id"],))[0]["version"] == 2


class TestWorkflow:
    def test_invalid_jump_is_rejected_and_item_untouched(self, w):
        item = w.create_item("memA1")
        r = w.call("POST", f"/api/items/{item['id']}/transition", "leadA", json={"to": "closed"})
        assert r.status_code == 409
        assert db("SELECT status, version FROM work_items WHERE id = %s", (item["id"],))[0] == {"status": "open", "version": 1}

    def test_only_a_lead_can_approve_and_the_full_path_is_recorded(self, w):
        item = w.create_item("memA1")

        def t(user, to):
            return w.call("POST", f"/api/items/{item['id']}/transition", user, json={"to": to})

        assert t("memA1", "in_progress").status_code == 200
        assert t("memA1", "awaiting_approval").status_code == 200

        # The detail endpoint only advertises transitions this user may actually take.
        as_member = w.call("GET", f"/api/items/{item['id']}", "memA1").json()
        as_lead = w.call("GET", f"/api/items/{item['id']}", "leadA").json()
        assert as_member["next_statuses"] == []
        assert sorted(as_lead["next_statuses"]) == ["in_progress", "resolved"]

        assert t("memA1", "resolved").status_code == 403  # creator is not a Lead
        assert t("viewA", "resolved").status_code == 403
        assert t("outsider", "resolved").status_code == 404
        assert db("SELECT status FROM work_items WHERE id = %s", (item["id"],))[0]["status"] == "awaiting_approval"

        assert t("leadA", "resolved").status_code == 200
        assert t("memA1", "closed").status_code == 200
        assert t("memA1", "in_progress").status_code == 409  # closed is terminal

        changes = [f"{e['data']['from']}>{e['data']['to']}" for e in events_for(item["id"]) if e["kind"] == "status_changed"]
        assert changes == ["open>in_progress", "in_progress>awaiting_approval", "awaiting_approval>resolved", "resolved>closed"]

    def test_stale_version_on_transition_is_rejected(self, w):
        item = w.create_item("memA1")
        w.call("PATCH", f"/api/items/{item['id']}", "memA1", json={"version": 1, "priority": "urgent"})
        r = w.call("POST", f"/api/items/{item['id']}/transition", "memA1", json={"to": "in_progress", "version": 1})
        assert r.status_code == 409


class TestAuthorization:
    def test_other_team_gets_404_on_every_read_and_write(self, w):
        item = w.create_item("memA1")
        i = item["id"]
        assert w.call("GET", f"/api/items/{i}", "outsider").status_code == 404
        assert w.call("GET", f"/api/items/{i}/activity", "outsider").status_code == 404
        assert w.call("PATCH", f"/api/items/{i}", "outsider", json={"version": 1, "title": "hax"}).status_code == 404
        assert w.call("POST", f"/api/items/{i}/claim", "outsider").status_code == 404
        assert w.call("POST", f"/api/items/{i}/comments", "outsider", json={"text": "hi"}).status_code == 404
        create = w.call("POST", "/api/items", "outsider", json={"title": "x", "type": "task", "priority": "low", "team_id": w.team_a})
        assert create.status_code == 400
        assert w.call("GET", f"/api/teams/{w.team_a}/members", "outsider").status_code == 404
        listing = w.call("GET", "/api/items?limit=100", "outsider").json()
        assert all(it["team_id"] == w.team_b for it in listing["items"])
        assert db("SELECT title FROM work_items WHERE id = %s", (i,))[0]["title"] == "Payment stuck"

    def test_viewers_can_read_but_not_write(self, w):
        item = w.create_item("memA1")
        i = item["id"]
        assert w.call("GET", f"/api/items/{i}", "viewA").status_code == 200
        assert w.call("POST", f"/api/items/{i}/claim", "viewA").status_code == 403
        assert w.call("PATCH", f"/api/items/{i}", "viewA", json={"version": 1, "title": "x"}).status_code == 403
        assert w.call("POST", f"/api/items/{i}/comments", "viewA", json={"text": "x"}).status_code == 403
        create = w.call("POST", "/api/items", "viewA", json={"title": "x", "type": "task", "priority": "low", "team_id": w.team_a})
        assert create.status_code == 403

    def test_member_cannot_touch_an_item_assigned_to_someone_else_but_a_lead_can(self, w):
        item = w.create_item("memA1")
        i = item["id"]
        w.call("POST", f"/api/items/{i}/claim", "memA1")
        assert w.call("POST", f"/api/items/{i}/transition", "memA2", json={"to": "in_progress"}).status_code == 403
        assert w.call("PATCH", f"/api/items/{i}", "memA2", json={"version": 2, "title": "nope"}).status_code == 403
        assert w.call("POST", f"/api/items/{i}/comments", "memA2", json={"text": "I can still comment"}).status_code == 201
        assert w.call("PATCH", f"/api/items/{i}", "leadA", json={"version": 2, "title": "lead edit"}).status_code == 200

    def test_only_leads_assign_others_and_only_to_team_workers(self, w):
        item = w.create_item("memA1")

        def patch(user, assignee):
            return w.call("PATCH", f"/api/items/{item['id']}", user, json={"version": 1, "assignee_id": assignee})

        assert patch("memA1", w.ids["memA2"]).status_code == 403
        assert patch("leadA", w.ids["viewA"]).status_code == 400
        assert patch("leadA", w.ids["outsider"]).status_code == 400
        ok = patch("leadA", w.ids["memA2"])
        assert ok.status_code == 200
        assert ok.json()["assignee_id"] == w.ids["memA2"]

    def test_requests_without_a_valid_token_get_401(self, w):
        assert w.call("GET", "/api/items").status_code == 401
        from fastapi.testclient import TestClient

        from app.main import app

        assert TestClient(app).get("/api/items", headers={"Authorization": "Bearer garbage"}).status_code == 401


class TestHistory:
    def test_records_each_kind_of_event_with_the_actor(self, w):
        item = w.create_item("memA1")
        i = item["id"]
        w.call("PATCH", f"/api/items/{i}", "leadA", json={"version": 1, "priority": "urgent", "assignee_id": w.ids["memA2"]})
        w.call("POST", f"/api/items/{i}/transition", "memA2", json={"to": "in_progress"})
        w.call("POST", f"/api/items/{i}/comments", "memA2", json={"text": "Looking into it"})
        ev = events_for(i)
        assert [e["kind"] for e in ev] == ["created", "priority_changed", "assignee_changed", "status_changed", "comment"]
        assert ev[1]["data"] == {"from": "high", "to": "urgent"}
        assert ev[2]["actor_id"] == w.ids["leadA"]

        feed = w.call("GET", f"/api/items/{i}/activity", "viewA")
        assert feed.status_code == 200
        assert feed.json()["events"][0]["kind"] == "comment"  # newest first

    def test_a_failed_change_leaves_neither_change_nor_event(self, w):
        item = w.create_item("memA1")
        before = len(events_for(item["id"]))
        # The priority edit is valid but the assignee is not, so the whole request must roll back.
        r = w.call("PATCH", f"/api/items/{item['id']}", "leadA", json={"version": 1, "priority": "urgent", "assignee_id": w.ids["outsider"]})
        assert r.status_code == 400
        assert len(events_for(item["id"])) == before
        assert db("SELECT priority, version FROM work_items WHERE id = %s", (item["id"],))[0] == {"priority": "high", "version": 1}

    def test_activity_events_is_append_only_in_the_database(self, w):
        import psycopg

        item = w.create_item("memA1")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            db("UPDATE activity_events SET kind = 'x' WHERE item_id = %s", (item["id"],))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            db("DELETE FROM activity_events WHERE item_id = %s", (item["id"],))


class TestListing:
    def test_cursor_pagination_visits_every_item_exactly_once(self, w):
        for n in range(23):
            w.create_item("memA1", title=f"Pagetest {n}")
        seen, cursor, pages = [], None, 0
        while True:
            url = "/api/items?q=pagetest&limit=10" + (f"&cursor={cursor}" if cursor else "")
            r = w.call("GET", url, "memA1")
            assert r.status_code == 200
            seen += [it["id"] for it in r.json()["items"]]
            cursor = r.json()["next_cursor"]
            pages += 1
            if not cursor:
                break
        assert pages == 3
        assert len(seen) == 23 and len(set(seen)) == 23

    def test_dashboard_views_are_computed_in_sql_and_respect_roles(self, w):
        w.create_item("memA1", priority="urgent", title="Urgent and unowned")
        urgent = w.call("GET", "/api/items?view=unassigned_urgent&limit=100", "memA1").json()
        assert urgent["items"]
        assert all(i["priority"] == "urgent" and not i["assignee_id"] for i in urgent["items"])

        item = w.create_item("memA1")
        w.call("POST", f"/api/items/{item['id']}/claim", "memA1")
        mine = w.call("GET", "/api/items?view=assigned_to_me", "memA1").json()
        assert item["id"] in [i["id"] for i in mine["items"]]

        w.call("POST", f"/api/items/{item['id']}/transition", "memA1", json={"to": "in_progress"})
        w.call("POST", f"/api/items/{item['id']}/transition", "memA1", json={"to": "awaiting_approval"})
        lead = w.call("GET", "/api/items?view=awaiting_my_approval", "leadA").json()
        assert item["id"] in [i["id"] for i in lead["items"]]
        member = w.call("GET", "/api/items?view=awaiting_my_approval", "memA1").json()
        assert member["items"] == []  # members cannot approve, so nothing waits on them

    def test_malformed_params_get_4xx_not_500(self, w):
        assert w.call("GET", "/api/items?limit=9999", "memA1").status_code == 400
        assert w.call("GET", "/api/items?status=bogus", "memA1").status_code == 400
        assert w.call("GET", "/api/items/not-a-uuid", "memA1").status_code == 404
