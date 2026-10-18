import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from .authz import AuthUser, allowed_actions, check, check_team
from .errors import bad_request, conflict, forbidden, not_found
from .http import get_user, run
from .outbox import enqueue
from .workflow import STATUSES, TRANSITIONS

router = APIRouter(prefix="/api/items")

Type = Literal["incident", "customer_issue", "payment", "compliance", "task"]
Priority = Literal["low", "medium", "high", "urgent"]
Status = Literal["open", "in_progress", "awaiting_approval", "resolved", "closed"]
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
UUID_RE = re.compile(UUID_PATTERN)
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

VIEW_SQL = """
  SELECT i.id, i.seq, i.title, i.description, i.type, i.priority, i.status, i.team_id, t.name AS team_name,
         i.assignee_id, a.name AS assignee_name, i.created_by, cu.name AS created_by_name,
         to_char(i.due_date, 'YYYY-MM-DD') AS due_date, i.version, i.created_at, i.updated_at
  FROM work_items i
  JOIN teams t ON t.id = i.team_id
  JOIN users cu ON cu.id = i.created_by
  LEFT JOIN users a ON a.id = i.assignee_id"""


# ---------- request bodies ----------
class CreateItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)
    type: Type
    priority: Priority
    team_id: str = Field(pattern=UUID_PATTERN)
    due_date: Optional[str] = Field(default=None, pattern=DATE_PATTERN)
    assignee_id: Optional[str] = Field(default=None, pattern=UUID_PATTERN)


class PatchItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    version: int = Field(gt=0)
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10000)
    priority: Optional[Priority] = None
    due_date: Optional[str] = Field(default=None, pattern=DATE_PATTERN)  # null clears it
    assignee_id: Optional[str] = Field(default=None, pattern=UUID_PATTERN)  # null unassigns


class TransitionBody(BaseModel):
    to: Status
    version: Optional[int] = Field(default=None, gt=0)
    note: Optional[str] = Field(default=None, max_length=2000)


class CommentBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=5000)


# ---------- helpers ----------
def get_view(conn, item_id: str):
    return conn.execute(VIEW_SQL + " WHERE i.id = %s", (item_id,)).fetchone()


def require(user: AuthUser, action: str, item) -> None:
    """Single enforcement point: 404 if the user is not in the team (don't leak existence), 403 if the role can't."""
    result = check(user, action, item)
    if result == "none":
        raise not_found("Item not found")
    if result == "denied":
        raise forbidden(f"Your role does not allow you to {action} this item")


def valid_id(item_id: str) -> str:
    if not UUID_RE.match(item_id):
        raise not_found("Item not found")
    return item_id


def log_event(conn, item_id: str, actor_id: str, kind: str, data: dict) -> None:
    """Every change writes its activity event through here, always on the caller's transaction."""
    conn.execute(
        "INSERT INTO activity_events (item_id, actor_id, kind, data) VALUES (%s, %s, %s, %s)",
        (item_id, actor_id, kind, Jsonb(data)),
    )


def assert_assignable(conn, team_id: str, user_id: str) -> None:
    m = conn.execute("SELECT role FROM memberships WHERE team_id = %s AND user_id = %s", (team_id, user_id)).fetchone()
    if not m or m["role"] == "viewer":
        raise bad_request("Assignee must be a Member, Lead or Admin of the item's team")


def lock_item(conn, item_id: str):
    row = conn.execute(
        "SELECT *, to_char(due_date, 'YYYY-MM-DD') AS due_date_str FROM work_items WHERE id = %s FOR UPDATE",
        (valid_id(item_id),),
    ).fetchone()
    if not row:
        raise not_found("Item not found")
    return row


def item_ref(conn, item_id: str):
    row = conn.execute(
        "SELECT team_id, assignee_id, created_by FROM work_items WHERE id = %s", (valid_id(item_id),)
    ).fetchone()
    if not row:
        raise not_found("Item not found")
    return row


# ---------- create ----------
@router.post("", status_code=201)
def create_item(body: CreateItem, request: Request, user: AuthUser = Depends(get_user)):
    def handler(conn):
        perm = check_team(user, "create", body.team_id)
        if perm == "none":
            raise bad_request("Unknown team")  # non-member: same answer as a nonexistent team
        if perm == "denied":
            raise forbidden("Your role does not allow you to create items in this team")
        if body.assignee_id:
            if check_team(user, "assign", body.team_id) != "ok":
                raise forbidden("Only Leads and Admins can assign items to others")
            assert_assignable(conn, body.team_id, body.assignee_id)
        item_id = conn.execute(
            """INSERT INTO work_items (title, description, type, priority, team_id, assignee_id, created_by, due_date)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (body.title, body.description, body.type, body.priority, body.team_id, body.assignee_id, user.id, body.due_date),
        ).fetchone()["id"]
        log_event(conn, item_id, user.id, "created", {"title": body.title, "priority": body.priority, "type": body.type})
        if body.assignee_id:
            log_event(conn, item_id, user.id, "assignee_changed", {"from": None, "to": body.assignee_id})
            if body.assignee_id != user.id:
                enqueue(conn, "notify_user", {"userId": body.assignee_id, "itemId": item_id, "message": f"You were assigned: {body.title}"}, f"assign:{item_id}:1")
        return 201, get_view(conn, item_id)

    return run(request, user, handler, body)


# ---------- list / dashboard: filtering, search and paging all happen in SQL ----------
@router.get("")
def list_items(
    request: Request,
    user: AuthUser = Depends(get_user),
    view: Optional[Literal["assigned_to_me", "unassigned_urgent", "awaiting_my_approval"]] = None,
    status: Optional[Status] = None,
    priority: Optional[Priority] = None,
    type: Optional[Type] = None,
    team_id: Optional[str] = Query(default=None, pattern=UUID_PATTERN),
    assignee: Optional[str] = None,  # 'me' | 'none' | uuid
    q: Optional[str] = Query(default=None, max_length=100),
    cursor: Optional[int] = Query(default=None, gt=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    def handler(conn):
        team_ids = list(user.roles.keys())
        if not team_ids:
            return 200, {"items": [], "next_cursor": None}

        where = ["i.team_id = ANY(%s::uuid[])"]  # the authorization scope, applied to every list query
        args: list = [team_ids]

        def add(clause: str, *values):
            where.append(clause)
            args.extend(values)

        if team_id:
            add("i.team_id = %s", team_id)
        if status:
            add("i.status = %s", status)
        if priority:
            add("i.priority = %s", priority)
        if type:
            add("i.type = %s", type)
        if assignee == "me":
            add("i.assignee_id = %s", user.id)
        elif assignee == "none":
            where.append("i.assignee_id IS NULL")
        elif assignee and UUID_RE.match(assignee):
            add("i.assignee_id = %s", assignee)

        if view == "assigned_to_me":
            add("i.assignee_id = %s", user.id)
            where.append("i.status NOT IN ('resolved','closed')")
        elif view == "unassigned_urgent":
            where.extend(["i.assignee_id IS NULL", "i.priority = 'urgent'", "i.status NOT IN ('resolved','closed')"])
        elif view == "awaiting_my_approval":
            approver_teams = [t for t in team_ids if check_team(user, "approve", t) == "ok"]
            where.append("i.status = 'awaiting_approval'")
            add("i.team_id = ANY(%s::uuid[])", approver_teams)

        if q:
            terms = re.findall(r"[^\W_]+", q.lower())[:8]
            if terms:
                add("i.search @@ to_tsquery('simple', %s)", " & ".join(f"{t}:*" for t in terms))
        if cursor:
            add("i.seq < %s", cursor)  # keyset pagination: stable and index-friendly

        args.append(limit + 1)
        rows = conn.execute(f"{VIEW_SQL} WHERE {' AND '.join(where)} ORDER BY i.seq DESC LIMIT %s", args).fetchall()
        more = len(rows) > limit
        page = rows[:limit]
        return 200, {"items": page, "next_cursor": int(page[-1]["seq"]) if more else None}

    return run(request, user, handler)


# ---------- detail ----------
@router.get("/{item_id}")
def get_item(item_id: str, request: Request, user: AuthUser = Depends(get_user)):
    def handler(conn):
        item = get_view(conn, valid_id(item_id))
        if not item:
            raise not_found("Item not found")
        require(user, "read", item)
        # The UI uses these to decide which buttons to show. The server re-checks every call anyway.
        next_statuses = [to for to, perm in TRANSITIONS[item["status"]].items() if check(user, perm, item) == "ok"]
        return 200, {**item, "allowed_actions": allowed_actions(user, item), "next_statuses": next_statuses}

    return run(request, user, handler)


# ---------- edit with optimistic locking ----------
@router.patch("/{item_id}")
def patch_item(item_id: str, body: PatchItem, request: Request, user: AuthUser = Depends(get_user)):
    def handler(conn):
        cur = lock_item(conn, item_id)  # row lock: concurrent writers queue here
        require(user, "read", cur)
        if cur["version"] != body.version:
            raise conflict("This item changed since you opened it. Reload to see the latest version.", current=get_view(conn, cur["id"]))
        if cur["status"] == "closed":
            raise conflict("Closed items cannot be edited")

        sent = body.model_fields_set
        sets: list[str] = []
        args: list = []
        events: list[tuple[str, dict]] = []
        edited: list[str] = []
        notify_assignee = None

        for k in ("title", "description", "due_date"):
            if k not in sent:
                continue
            v = getattr(body, k)
            if v is None and k != "due_date":
                continue
            before = cur["due_date_str"] if k == "due_date" else cur[k]
            if v != before:
                sets.append(f"{k} = %s")
                args.append(v)
                edited.append(k)
        if body.priority is not None and body.priority != cur["priority"]:
            sets.append("priority = %s")
            args.append(body.priority)
            events.append(("priority_changed", {"from": cur["priority"], "to": body.priority}))
            edited.append("priority")
        if "assignee_id" in sent and body.assignee_id != cur["assignee_id"]:
            require(user, "assign", cur)
            if body.assignee_id:
                assert_assignable(conn, cur["team_id"], body.assignee_id)
            sets.append("assignee_id = %s")
            args.append(body.assignee_id)
            events.append(("assignee_changed", {"from": cur["assignee_id"], "to": body.assignee_id}))
            if body.assignee_id and body.assignee_id != user.id:
                notify_assignee = body.assignee_id
            edited.append("assignee_id")

        if not sets:
            return 200, get_view(conn, cur["id"])  # nothing changed: no new version

        if any(f != "assignee_id" for f in edited):
            require(user, "edit", cur)  # plain-field edits (assignment was checked above)
        other = [f for f in edited if f not in ("priority", "assignee_id")]
        if other:
            events.insert(0, ("edited", {"fields": other}))

        conn.execute(
            f"UPDATE work_items SET {', '.join(sets)}, version = version + 1, updated_at = now() WHERE id = %s",
            [*args, cur["id"]],
        )
        for kind, data in events:
            log_event(conn, cur["id"], user.id, kind, data)
        if notify_assignee:
            title = body.title or cur["title"]
            enqueue(conn, "notify_user", {"userId": notify_assignee, "itemId": cur["id"], "message": f"You were assigned: {title}"}, f"assign:{cur['id']}:{cur['version'] + 1}")
        return 200, get_view(conn, cur["id"])

    return run(request, user, handler, body)


# ---------- atomic claim ----------
@router.post("/{item_id}/claim")
def claim_item(item_id: str, request: Request, user: AuthUser = Depends(get_user)):
    def handler(conn):
        require(user, "claim", item_ref(conn, item_id))
        # ONE conditional UPDATE decides the winner. Under READ COMMITTED the loser waits for the
        # winner's row lock, re-evaluates "assignee_id IS NULL", matches nothing and gets 0 rows.
        won = conn.execute(
            """UPDATE work_items SET assignee_id = %s, version = version + 1, updated_at = now()
               WHERE id = %s AND assignee_id IS NULL AND status IN ('open','in_progress','awaiting_approval')
               RETURNING id""",
            (user.id, item_id),
        ).fetchone()
        if not won:
            latest = get_view(conn, item_id)
            why = f"Already claimed by {latest['assignee_name']}" if latest["assignee_id"] else "This item can no longer be claimed"
            raise conflict(why, current=latest)
        log_event(conn, item_id, user.id, "assignee_changed", {"from": None, "to": user.id, "via": "claim"})
        return 200, get_view(conn, item_id)

    return run(request, user, handler)


# ---------- workflow transition ----------
@router.post("/{item_id}/transition")
def transition_item(item_id: str, body: TransitionBody, request: Request, user: AuthUser = Depends(get_user)):
    def handler(conn):
        cur = lock_item(conn, item_id)
        require(user, "read", cur)
        needed = TRANSITIONS[cur["status"]].get(body.to)
        if not needed:
            raise conflict(f'Cannot move an item from "{cur["status"]}" to "{body.to}"', current=get_view(conn, cur["id"]))
        require(user, needed, cur)  # e.g. awaiting_approval -> resolved needs "approve" (Lead/Admin only)
        if body.version is not None and body.version != cur["version"]:
            raise conflict("This item changed since you opened it. Reload to see the latest version.", current=get_view(conn, cur["id"]))

        conn.execute("UPDATE work_items SET status = %s, version = version + 1, updated_at = now() WHERE id = %s", (body.to, cur["id"]))
        data = {"from": cur["status"], "to": body.to}
        if body.note:
            data["note"] = body.note
        log_event(conn, cur["id"], user.id, "status_changed", data)
        if body.to == "resolved" and cur["created_by"] != user.id:
            enqueue(conn, "notify_user", {"userId": cur["created_by"], "itemId": cur["id"], "message": f"Resolved: {cur['title']}"}, f"resolved:{cur['id']}:{cur['version'] + 1}")
        return 200, get_view(conn, cur["id"])

    return run(request, user, handler, body)


# ---------- comments + activity ----------
@router.post("/{item_id}/comments", status_code=201)
def add_comment(item_id: str, body: CommentBody, request: Request, user: AuthUser = Depends(get_user)):
    def handler(conn):
        require(user, "comment", item_ref(conn, item_id))
        log_event(conn, item_id, user.id, "comment", {"text": body.text})
        return 201, {"ok": True}

    return run(request, user, handler, body)


@router.get("/{item_id}/activity")
def get_activity(
    item_id: str,
    request: Request,
    user: AuthUser = Depends(get_user),
    before: Optional[int] = Query(default=None, gt=0),
    limit: int = Query(default=30, ge=1, le=100),
):
    def handler(conn):
        require(user, "read", item_ref(conn, item_id))
        args: list = [item_id]
        cond = ""
        if before:
            cond = "AND e.id < %s"
            args.append(before)
        args.append(limit + 1)
        rows = conn.execute(
            f"""SELECT e.id, e.kind, e.data, e.created_at, e.actor_id, u.name AS actor_name
                FROM activity_events e JOIN users u ON u.id = e.actor_id
                WHERE e.item_id = %s {cond} ORDER BY e.id DESC LIMIT %s""",
            args,
        ).fetchall()
        more = len(rows) > limit
        page = rows[:limit]
        return 200, {"events": page, "next_before": int(page[-1]["id"]) if more else None}

    return run(request, user, handler)
