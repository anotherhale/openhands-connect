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
#: because that is exactly the entry-dropping bug it fixes.
CURSOR_VERSION = 2

#: A real cursor is a small base64 blob. Bounding the length keeps a caller from
#: forcing large decode work on every page.
MAX_CURSOR_CHARS = 1024


def encode_cursor(
    *,
    task_id: str,
    environment_id: str,
    sort_order: LogSortOrder,
    upstream_page_id: str,
    boundary: str | None = None,
    skip: int = 0,
) -> str:
    """Wrap an upstream ``next_page_id`` in a bridge cursor.

    ``boundary`` is the timestamp of the last item on the page just returned; the
    next page must begin strictly past it in the traversal's direction. It
    travels in the cursor because upstream will happily answer a stale
    ``page_id`` with page 1 and ``200``; without the boundary the bridge cannot
    tell "next page" from "silently restarted at page 1".

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
    if boundary is not None:
        payload["b"] = boundary
    if skip:
        payload["s"] = skip
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(
    cursor: str, *, task_id: str, environment_id: str
) -> tuple[LogSortOrder, str, str | None, int]:
    """Validate a cursor and return its direction, page id, bound, and skip.

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

    boundary = payload.get("b")
    if boundary is not None and not isinstance(boundary, str):
        raise InvalidInputError("cursor has an invalid boundary")

    skip = payload.get("s", 0)
    # ``bool`` is an ``int`` subclass, so exclude it explicitly: True would
    # otherwise be accepted as a skip of 1.
    if not isinstance(skip, int) or isinstance(skip, bool) or skip < 0:
        raise InvalidInputError("cursor has an invalid resume position")

    return order, page_id, boundary, skip


__all__ = ["CURSOR_VERSION", "MAX_CURSOR_CHARS", "decode_cursor", "encode_cursor"]
