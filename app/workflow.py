"""The state machine. Each edge names the permission needed to take it; anything not listed is a 409.

  open --> in_progress --> awaiting_approval --(Lead)--> resolved --> closed
    ^            |  ^______________|(Lead rejects)           |
    |____________|                                           |
                 ^___________________(reopen)_______________|
"""

STATUSES = ("open", "in_progress", "awaiting_approval", "resolved", "closed")

TRANSITIONS: dict[str, dict[str, str]] = {
    "open": {"in_progress": "transition"},
    "in_progress": {"open": "transition", "awaiting_approval": "transition"},
    "awaiting_approval": {"resolved": "approve", "in_progress": "approve"},
    "resolved": {"closed": "transition", "in_progress": "transition"},
    "closed": {},
}
