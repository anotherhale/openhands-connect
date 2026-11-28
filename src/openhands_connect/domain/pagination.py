"""Opaque, validated cursors for task-log pagination.

Why this exists: the upstream ``events/search`` route **silently ignores** an
invalid or foreign ``page_id`` and returns page 1 with ``HTTP 200`` (verified on
Agent Server 1.60.1). Trusting upstream to reject a bad cursor therefore means a
caller that reuses a cursor from another task — or a stale one — silently gets
the *newest* page again instead of an error, and can loop forever without
noticing. So the bridge wraps the upstream token in a cursor it can validate
itself and binds it to the task, environment, and sort direction it was issued
for.

This is a correctness boundary, not a security one. A caller can already read
any task it names, so a forged cursor grants nothing it did not already have;
what matters is that an *accidental* mismatch is detected rather than quietly
returning the wrong page. No secret is involved, so do not describe this as
authenticated.
"""

from __future__ import annotations

import base64
import binascii
import json

from openhands_connect.domain.errors import InvalidInputError
from openhands_connect.domain.models import LogSortOrder

#: Bumped when the payload shape changes; an older cursor then fails validation
#: instead of being misread under the new shape. Version 2 added ``s``: a cursor
#: from before that must not be read as "offset 0" by a bridge that knows better,
#: because that is exactly the entry-dropping bug it fixes. Version 3 dropped
#: ``b`` (a timestamp boundary) because timestamps are not monotonic across pages
#: and the timestamp guard truncated traversals; the page token is now its own
#: anchor, so a cursor from version 2 or earlier must not be read under it.
CURSOR_VERSION = 3

#: A real cursor is a small base64 blob. Bounding the length keeps a caller from
#: forcing large decode work on every page.
MAX_CURSOR_CHARS = 1024


def encode_cursor(
    *,
    task_id: str,
    environment_id: str,
    sort_order: LogSortOrder,
    upstream_page_id: str,
    skip: int = 0,
) -> str:
    """Wrap an upstream ``next_page_id`` in a bridge cursor.

    Upstream's page token is itself the id of the next page's first item
    (verified on Agent Server 1.60.1, both sort directions), so requesting it
    returns a page whose first item carries that same id. The adapter validates
    that equality, which is what detects upstream silently restarting at page 1
    for a stale or foreign token. Nothing extra needs to travel in the cursor:
    the token is its own anchor.

    Verifying by *id* rather than by timestamp is the point. A timestamp boundary
    cannot order this history: upstream emits events whose timestamps are not
    monotonic even across page boundaries (a hook event arrives after the state
    update it caused but carries an earlier timestamp), so a directional
    timestamp guard stops a legitimate traversal early. Ids are a stable,
    server-minted identity.

    ``skip`` is how many renderable entries at the *start* of the page named by
    ``upstream_page_id`` were already returned to the caller. A single response
    can stop partway through a fetched page when the page held more entries than
    the response had room for, and the caller must resume at the remainder
    rather than after it - otherwise those entries are never returned at all.
    """
    payload: dict[str, object] = {
        "v": CURSOR_VERSION,
        "t": task_id,
        "e": environment_id,
        "o": sort_order.value,
        "p": upstream_page_id,
    }
    if skip:
        payload["s"] = skip
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(
    cursor: str, *, task_id: str, environment_id: str
) -> tuple[LogSortOrder, str, int]:
    """Validate a cursor and return its direction, page token, and skip.

    Rejects, as ``InvalidInputError``: an empty or over-long cursor, one that is
    not a well-formed envelope, one issued for a different task or environment,
    one from another cursor version, and one with a nonsensical skip. None of
    these can be left to upstream, which would answer ``200`` with page 1.
    """
    if not cursor:
        raise InvalidInputError("cursor is empty")
    if len(cursor) > MAX_CURSOR_CHARS:
        raise InvalidInputError("cursor is too long to be valid")

    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode())
    except (binascii.Error, ValueError) as exc:
        raise InvalidInputError("cursor is not valid base64") from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidInputError("cursor is not a valid cursor payload") from exc

    if not isinstance(payload, dict):
        raise InvalidInputError("cursor is not a valid cursor payload")

    version = payload.get("v")
    if version != CURSOR_VERSION:
        raise InvalidInputError(
            f"cursor version {version!r} is not supported; re-request the newest page"
        )

    cursor_task = payload.get("t")
    cursor_env = payload.get("e")
    if cursor_task != task_id or cursor_env != environment_id:
        # Deliberately does not echo the cursor's task id: it is caller-supplied
        # data and belongs to another task, so it must not leak into this task's
        # error output.
        raise InvalidInputError(
            "cursor was issued for a different task or environment; "
            "request the newest page for this task to start a new traversal"
        )

    raw_order = payload.get("o")
    if not isinstance(raw_order, str):
        raise InvalidInputError("cursor has an unrecognized sort order")
    try:
        order = LogSortOrder(raw_order)
    except ValueError as exc:
        raise InvalidInputError("cursor has an unrecognized sort order") from exc

    page_id = payload.get("p")
    if not isinstance(page_id, str) or not page_id:
        raise InvalidInputError("cursor has no usable page token")

    skip = payload.get("s", 0)
    # ``bool`` is an ``int`` subclass, so exclude it explicitly: True would
    # otherwise be accepted as a skip of 1.
    if not isinstance(skip, int) or isinstance(skip, bool) or skip < 0:
        raise InvalidInputError("cursor has an invalid resume position")

    return order, page_id, skip


__all__ = ["CURSOR_VERSION", "MAX_CURSOR_CHARS", "decode_cursor", "encode_cursor"]
