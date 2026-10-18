# Engineering decisions

Each decision states the context, the options considered, what I chose, and the trade-off.

## 0. Assumptions I made (I would confirm these with a product manager first)

1. **A work item belongs to exactly one team.** Roles are per team, so a user can be a Lead in one team and a Viewer in another.
2. **Four roles:** Viewer (read), Member (work on items), Lead (approve, assign others), Admin (Lead + reserved for team management, not built).
3. **Workflow:** `open -> in_progress -> awaiting_approval -> resolved -> closed`, with Lead rejection back to `in_progress` and reopen from `resolved`. `closed` is terminal.
4. **"Claim" means taking an unassigned item for yourself.** Assigning to *someone else* is a Lead/Admin action.
5. **A Member may edit or move an item only if it is unassigned or it is theirs** (assigned to or created by them). Anyone in the team can still comment.
6. **Non-members get 404, not 403,** so the existence of other teams' items is not revealed.
7. **Notifications** happen when you are assigned an item or when something you created is resolved.
8. A Lead may approve their own item (no four-eyes rule). This is the first thing I would ask about.

## 1. Concurrency: a conditional UPDATE for claims, a row lock plus version check for edits

**Context.** Two people can click "Claim" at the same moment; two people can edit the same item from stale screens.

**Options.** (a) Read, check in application code, then write. This is a race: both reads see "unassigned". (b) Serializable transactions with retry on
failure. (c) A single conditional `UPDATE ... WHERE assignee_id IS NULL`. (d) Optimistic locking with a `version` column. (e) Pessimistic
`SELECT ... FOR UPDATE`.

**Choice.**
* **Claim** uses (c): `UPDATE work_items SET assignee_id = $me ... WHERE id = $1 AND assignee_id IS NULL AND status IN (...)`.
  Under Postgres READ COMMITTED the second claimer waits for the first one's row lock, re-checks the `WHERE`, matches nothing and gets
  0 rows, which becomes a 409 carrying the current item ("Already claimed by X"). One statement decides the winner, so there is nothing to race.
* **Edits and transitions** use (d)+(e): the client sends the `version` it saw; the server locks the row with `FOR UPDATE`, compares
  versions and rejects a mismatch with 409 plus the latest item. Locking first means the permission check, the version check and the write all
  see the same row, and two simultaneous edits are serialised instead of both passing the check.

**Trade-off.** Row locks make concurrent writers to the *same item* queue briefly (fine: edits are rare and short). Different items never
contend. I prefer a 409 the user can understand over silently merging changes. Transitions take an *optional* version, so a double-clicked "Start work" doesn't fail unnecessarily
but a decision made on a stale screen (e.g. approving) can be protected by sending it.

## 2. Idempotency: the key is claimed inside the same transaction as the work

**Context.** Double-clicks, flaky networks and client retries must not create two items or apply a change twice.

**Options.** (a) Disable the button only. (b) Check-then-insert a key in middleware before the handler. (c) Insert the key as the first
statement of the request's own transaction and store the response with it.

**Choice.** (c). `INSERT ... ON CONFLICT (user_id, key) DO NOTHING` runs inside the handler's transaction.
* First request: runs the handler, stores `(status, body)` against the key, commits both atomically.
* Sequential repeat: the insert conflicts, so the stored response is replayed (`Idempotent-Replayed: true`).
* **Concurrent** repeat: the second insert blocks on the unique index until the first commits, then replays; it never runs the handler.
* Same key but different request body: 422 (a client bug, not a retry).
* Failed requests roll back *including the key*, so "fix the input and retry with the same key" works.

Keys are scoped per user. The UI generates one key per user action and retries a dropped connection once with the same key.

**Trade-off.** Every keyed request does one extra insert/update, and keys accumulate (they need a periodic purge, listed under next steps).
Because the key and the work commit together there is no window where one exists without the other, which is the whole point.

## 3. Authorization: one function, enforced on the server, scoped by team and resource

**Context.** Hidden buttons are not security. The rules involve role-per-team and a resource-level rule (a Member can't touch someone else's assigned item).

**Options.** (a) Role checks scattered in route handlers. (b) A middleware per route using only the user's global role. (c) One pure `check(user, action, item)` over a role x action matrix,
called by every route after the item is loaded, plus a team scope on every list query.

**Choice.** (c). `app/authz.py` holds the matrix and the resource rule; routes call it through a single wrapper (`require`) that maps the result
to 404 (not in team) or 403 (in team, not allowed). Lists apply `team_id = ANY(user's teams)` in SQL, and the "awaiting my approval" view
further restricts to teams where the user can approve. Roles are loaded from the database on **every** request (not baked into the token), so demoting someone takes effect immediately.
The detail endpoint returns `allowed_actions` and `next_statuses` so the UI shows only valid buttons, but this is a convenience: the server re-checks every call.

**Trade-off.** One extra query per request to load roles (two small indexed lookups). Being pure and table-driven, the matrix is easy to unit-test and to change live
("let Leads reassign" is one line). The cost of 404-for-outsiders is slightly less helpful error messages.

## 4. Sync vs async: a transactional outbox with an idempotent worker

**Context.** Telling someone they were assigned an item should happen reliably, but must not slow down or fail the request. Writing the change
and then calling a queue/email service is a dual write: a crash between the two loses or invents notifications.

**Options.** (a) Send inline in the request. (b) Commit, then enqueue to Redis/BullMQ. (c) Write a job row in the *same* transaction as the change
(outbox) and let a worker deliver it.

**Choice.** (c), Postgres-only to keep setup to one container (no Redis or Celery). The job row is inserted with the change, so they commit or roll back together
(tested: a rejected edit leaves no job). The worker claims jobs with `FOR UPDATE SKIP LOCKED` (multiple workers never take the same job; tested), runs the
handler, and then marks `done`, reschedules with exponential backoff, or marks `failed` after `max_attempts`. A job stuck in `processing` for 60 s (worker crash)
becomes runnable again. Delivery is **at-least-once**, so the handler is idempotent: `notifications.dedupe_key` is UNIQUE and the insert is `ON CONFLICT DO NOTHING`, hence running a job twice still gives one notification.

**Trade-off.** Polling (1 s when idle) adds up to a second of latency and some idle queries; a dedicated broker or `LISTEN/NOTIFY` would be snappier. Postgres as the queue
is simple and transactional, but it shares capacity with the OLTP workload. At much larger volume I would move to a real queue and keep the outbox as the source of truth.

## 5. Search, filtering and pagination: all in SQL, keyset cursors, indexes shaped like the dashboard

**Context.** Tens of thousands of items and a growing history; loading everything into the browser is not an option.

**Options.** (a) Offset/limit. (b) Keyset ("seek") pagination. (c) A search engine.

**Choice.** (b) on a monotonic `seq` column (`WHERE seq < $cursor ORDER BY seq DESC LIMIT n+1`). It stays O(page) at any depth, and does not skip or repeat rows when
items are inserted while paging (offset would). I used `seq` rather than `created_at` because timestamps can tie or lose precision between Postgres and JavaScript.
Search is Postgres full-text over a generated `tsvector` column with a GIN index, with prefix matching (`refu:* & stuc:*`). Indexes follow the dashboard queries:
* `(team_id, status, seq DESC)` for team/status filters,
* a **partial** index for "unassigned urgent" (`WHERE assignee_id IS NULL AND priority='urgent' AND status NOT IN (...)`),
* a partial index for `awaiting_approval`, one on `(assignee_id, seq DESC)` for "assigned to me", and GIN for search.

I checked with `EXPLAIN ANALYZE` on the 5,000 seeded rows that "unassigned urgent" uses its partial index (about 0.2 ms).

**Trade-off.** Keyset paging gives "next page" but no "jump to page 17" or a total count (counting is expensive at scale). Indexes cost write speed and disk. Full-text with the
`simple` config has no stemming or typo tolerance; fine here, and Postgres is the system of record anyway.

## What I chose not to build

Email delivery, attachments, SSO, websockets (the UI refetches/polls), sign-up, team administration screens and a fancy UI, per the brief. The front end is plain HTML, CSS and JavaScript with no framework or build step, which keeps the repo to one `docker compose up` and lets the time go to the server behaviours the brief grades. The backend is Python (FastAPI) with hand-written SQL through psycopg, so the locking and transaction logic stays visible instead of hidden behind an ORM.

## What would change at 10x scale

* **Reads:** a read replica for list/search traffic; keep claims and edits on the primary.
* **History:** partition `activity_events` by month; it is append-only and read by `(item_id, id)`, so old partitions can be archived cheaply.
* **Search:** move to OpenSearch/Elastic if ranking, typo tolerance or cross-field relevance matter.
* **Async:** a dedicated queue (SQS/Redis) with the outbox kept as the durable source; several worker replicas already work thanks to `SKIP LOCKED`.
* **Freshness:** server-sent events or websockets instead of polling.
* **Housekeeping:** purge old idempotency keys and finished outbox rows on a schedule.

## Known limitations

See the "Known limitations" section of the [README](README.md).
