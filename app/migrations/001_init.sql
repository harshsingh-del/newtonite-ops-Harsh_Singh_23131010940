CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL UNIQUE,
  name          text NOT NULL,
  password_hash text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE teams (
  id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE
);

-- One row per (user, team): a user can have a different role in each team.
CREATE TABLE memberships (
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  team_id uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  role    text NOT NULL CHECK (role IN ('viewer','member','lead','admin')),
  PRIMARY KEY (user_id, team_id)
);
CREATE INDEX memberships_team_idx ON memberships(team_id);

CREATE TABLE work_items (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seq         bigserial NOT NULL UNIQUE,            -- monotonic key for cursor pagination
  title       text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  description text NOT NULL DEFAULT '',
  type        text NOT NULL CHECK (type IN ('incident','customer_issue','payment','compliance','task')),
  priority    text NOT NULL CHECK (priority IN ('low','medium','high','urgent')),
  status      text NOT NULL DEFAULT 'open'
              CHECK (status IN ('open','in_progress','awaiting_approval','resolved','closed')),
  team_id     uuid NOT NULL REFERENCES teams(id),
  assignee_id uuid REFERENCES users(id),
  created_by  uuid NOT NULL REFERENCES users(id),
  due_date    date,
  version     integer NOT NULL DEFAULT 1,           -- optimistic locking
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  search      tsvector GENERATED ALWAYS AS (
                to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(description,''))
              ) STORED
);

-- Indexes chosen for the dashboard queries (see ENGINEERING_DECISIONS.md).
CREATE INDEX items_team_status_seq_idx ON work_items (team_id, status, seq DESC);
CREATE INDEX items_assignee_seq_idx    ON work_items (assignee_id, seq DESC) WHERE assignee_id IS NOT NULL;
CREATE INDEX items_unassigned_urgent_idx ON work_items (team_id, seq DESC)
  WHERE assignee_id IS NULL AND priority = 'urgent' AND status NOT IN ('resolved','closed');
CREATE INDEX items_awaiting_idx ON work_items (team_id, seq DESC) WHERE status = 'awaiting_approval';
CREATE INDEX items_search_idx ON work_items USING gin (search);

-- Append-only history. Comments are events of kind 'comment'.
CREATE TABLE activity_events (
  id         bigserial PRIMARY KEY,
  item_id    uuid NOT NULL REFERENCES work_items(id),
  actor_id   uuid NOT NULL REFERENCES users(id),
  kind       text NOT NULL,   -- created | status_changed | assignee_changed | priority_changed | edited | comment
  data       jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_item_idx ON activity_events (item_id, id DESC);

CREATE FUNCTION forbid_activity_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'activity_events is append-only';
END $$ LANGUAGE plpgsql;
CREATE TRIGGER activity_no_update BEFORE UPDATE OR DELETE ON activity_events
  FOR EACH ROW EXECUTE FUNCTION forbid_activity_mutation();

-- Idempotency: the first request with a key stores its response; repeats replay it.
CREATE TABLE idempotency_keys (
  user_id      uuid NOT NULL,
  key          text NOT NULL,
  request_hash text NOT NULL,
  status_code  integer,
  response     jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);

-- Transactional outbox: written in the same transaction as the change that caused it.
CREATE TABLE outbox (
  id           bigserial PRIMARY KEY,
  type         text NOT NULL,
  payload      jsonb NOT NULL,
  dedupe_key   text NOT NULL UNIQUE,
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','failed')),
  attempts     integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 5,
  next_run_at  timestamptz NOT NULL DEFAULT now(),
  locked_at    timestamptz,
  last_error   text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_runnable_idx ON outbox (next_run_at) WHERE status IN ('pending','processing');

-- The "effect" of a job. UNIQUE(dedupe_key) makes the handler idempotent.
CREATE TABLE notifications (
  id         bigserial PRIMARY KEY,
  user_id    uuid NOT NULL REFERENCES users(id),
  item_id    uuid REFERENCES work_items(id),
  message    text NOT NULL,
  dedupe_key text NOT NULL UNIQUE,
  read       boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX notifications_user_idx ON notifications (user_id, id DESC);
