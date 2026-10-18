from dataclasses import dataclass, field

ACTIONS = ("read", "create", "comment", "edit", "claim", "transition", "approve", "assign")

# Role x action matrix: the single source of truth for permissions.
MATRIX: dict[str, set[str]] = {
    "viewer": {"read"},
    "member": {"read", "create", "comment", "edit", "claim", "transition"},
    "lead": set(ACTIONS),
    "admin": set(ACTIONS),
}


@dataclass
class AuthUser:
    id: str
    email: str
    name: str
    roles: dict[str, str] = field(default_factory=dict)  # team_id -> role, loaded fresh on every request


def check(user: AuthUser, action: str, item) -> str:
    """THE permission check. `item` needs team_id, assignee_id, created_by.

    Returns "none" (not in the item's team: caller answers 404 so existence isn't leaked),
    "denied" (in the team but the role lacks the right: 403) or "ok".
    Resource-level rule: a plain Member may edit/transition an item only if it is unassigned or
    they created / are assigned to it. Leads and Admins may act on any item in their team.
    """
    role = user.roles.get(item["team_id"])
    if role is None:
        return "none"
    if action not in MATRIX[role]:
        return "denied"
    if role == "member" and action in ("edit", "transition"):
        mine = item["assignee_id"] == user.id or item["created_by"] == user.id
        if not mine and item["assignee_id"] is not None:
            return "denied"
    return "ok"


def check_team(user: AuthUser, action: str, team_id: str) -> str:
    """Team-level check when there is no item yet (creating, listing members)."""
    role = user.roles.get(team_id)
    if role is None:
        return "none"
    return "ok" if action in MATRIX[role] else "denied"


def allowed_actions(user: AuthUser, item) -> list[str]:
    """What the UI may offer. The server re-checks every call regardless."""
    return [a for a in ACTIONS if check(user, a, item) == "ok"]


def load_user(conn, user_id: str) -> AuthUser | None:
    u = conn.execute("SELECT id, email, name FROM users WHERE id = %s", (user_id,)).fetchone()
    if not u:
        return None
    rows = conn.execute("SELECT team_id, role FROM memberships WHERE user_id = %s", (user_id,)).fetchall()
    return AuthUser(id=u["id"], email=u["email"], name=u["name"], roles={r["team_id"]: r["role"] for r in rows})
