import bcrypt

from .db import pool

DEMO_PASSWORD = "password123"

TEAMS = ["Payments Ops", "Customer Support", "Compliance", "Platform Incidents"]

# (email, name, {team: role})
USERS = [
    ("admin@demo.test", "Ada Admin", {t: "admin" for t in TEAMS}),
    ("lead.pay@demo.test", "Priya Lead (Payments)", {"Payments Ops": "lead", "Platform Incidents": "member"}),
    ("lead.support@demo.test", "Sam Lead (Support)", {"Customer Support": "lead"}),
    ("lead.comp@demo.test", "Carla Lead (Compliance)", {"Compliance": "lead", "Payments Ops": "viewer"}),
    ("member.a@demo.test", "Mia Member", {"Payments Ops": "member", "Customer Support": "member"}),
    ("member.b@demo.test", "Ben Member", {"Payments Ops": "member"}),
    ("member.c@demo.test", "Chen Member", {"Customer Support": "member", "Compliance": "member"}),
    ("member.d@demo.test", "Dee Member", {"Platform Incidents": "member"}),
    ("viewer.a@demo.test", "Vik Viewer", {"Payments Ops": "viewer", "Customer Support": "viewer"}),
    ("multi@demo.test", "Maya Multi-role", {"Payments Ops": "member", "Compliance": "lead", "Platform Incidents": "viewer"}),
]

# NB: '%%' because psycopg treats a single '%' as a parameter marker.
BULK_SQL = """
WITH workers AS (
  SELECT m.team_id, array_agg(m.user_id) AS ids FROM memberships m WHERE m.role <> 'viewer' GROUP BY m.team_id
), team_list AS (
  SELECT team_id, ids, row_number() OVER (ORDER BY team_id) AS rn FROM workers
), gen AS (
  SELECT g, (SELECT count(*) FROM team_list) AS n_teams,
         (ARRAY['incident','customer_issue','payment','compliance','task'])[1 + floor(random()*5)::int] AS type,
         (ARRAY['low','medium','medium','high','urgent'])[1 + floor(random()*5)::int] AS priority,
         (ARRAY['open','open','in_progress','in_progress','awaiting_approval','resolved','closed'])[1 + floor(random()*7)::int] AS status,
         (ARRAY['Refund stuck','Checkout failing','Chargeback review','KYC document missing','Login outage','Duplicate invoice','Settlement mismatch','Audit evidence request','Slow dashboard','Webhook retries','Fraud alert','Card declined'])[1 + floor(random()*12)::int] AS topic,
         random() AS r_assign, random() AS r_due
  FROM generate_series(1, %s::int) g
), picked AS (
  SELECT gen.*, tl.team_id, tl.ids FROM gen JOIN team_list tl ON tl.rn = 1 + (gen.g %% gen.n_teams)
), ins AS (
  INSERT INTO work_items (title, description, type, priority, status, team_id, assignee_id, created_by, due_date, created_at, updated_at)
  SELECT topic || ' #' || g, 'Seeded sample item ' || g || ' about ' || lower(topic) || '.',
         type, priority, status, team_id,
         CASE WHEN r_assign < 0.35 THEN NULL ELSE ids[1 + floor(random()*cardinality(ids))::int] END,
         ids[1 + floor(random()*cardinality(ids))::int],
         CASE WHEN r_due < 0.5 THEN (current_date + (floor(random()*30) - 10)::int) ELSE NULL END,
         now() - (g || ' minutes')::interval, now() - (g || ' minutes')::interval
  FROM picked ORDER BY g RETURNING id, created_by, title, priority, type
)
INSERT INTO activity_events (item_id, actor_id, kind, data)
SELECT id, created_by, 'created', jsonb_build_object('title', title, 'priority', priority, 'type', type) FROM ins
"""


def seed(item_count: int = 5000) -> None:
    """Demo teams/users/roles, then ~5,000 items so pagination and indexes are exercised for real."""
    with pool.connection() as conn:
        if conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"] > 0:
            print("seed: data already present, skipping")
            return
        pw_hash = bcrypt.hashpw(DEMO_PASSWORD.encode(), bcrypt.gensalt(rounds=10)).decode()
        team_ids = {
            name: conn.execute("INSERT INTO teams (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
            for name in TEAMS
        }
        for email, name, roles in USERS:
            uid = conn.execute(
                "INSERT INTO users (email, name, password_hash) VALUES (%s,%s,%s) RETURNING id", (email, name, pw_hash)
            ).fetchone()["id"]
            for team, role in roles.items():
                conn.execute("INSERT INTO memberships (user_id, team_id, role) VALUES (%s,%s,%s)", (uid, team_ids[team], role))
        conn.execute(BULK_SQL, (item_count,))
    print(f"seed: created {len(TEAMS)} teams, {len(USERS)} users, {item_count} items. Password for all users: {DEMO_PASSWORD}")


if __name__ == "__main__":
    seed()
    pool.close()
