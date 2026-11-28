"""Pagination tests for ``get_task_logs``.

Every upstream behaviour asserted here was observed on a live Agent Server
1.60.1 (see ``docs/adr/0002-openhands-agent-server-api.md``):

* ``sort_order=TIMESTAMP_DESC`` returns the newest event first.
* ``next_page_id`` pages backward losslessly.
* An invalid or foreign ``page_id`` is **silently ignored** and answers ``200``
  with page 1 — so the bridge must validate cursors itself and must not repeat
  a page it has already returned.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import httpx
import pytest

from openhands_connect.adapters.openhands import MAX_LOG_PAGES_PER_CALL, OpenHandsAdapter
from openhands_connect.domain.errors import InvalidInputError
from openhands_connect.domain.models import Environment, LogSortOrder
from openhands_connect.redaction import Redactor

Handler = Callable[[httpx.Request], httpx.Response]


def adapter_for(handler: Handler, environment: Environment) -> OpenHandsAdapter:
    return OpenHandsAdapter(
        lambda _env: "secret-token",
        transport=httpx.MockTransport(handler),
        redactor=Redactor(("secret-token",)),
    )


def event(event_id: str, timestamp: str, text: str = "hello") -> dict[str, object]:
    return {
        "id": event_id,
        "timestamp": timestamp,
        "kind": "MessageEvent",
        "source": "agent",
        "llm_message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def page(items: list[dict[str, object]], next_page_id: str | None) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "next_page_id": next_page_id})


# A three-page history, newest first, as upstream would return it.
PAGE_1 = [event("e6", "2026-11-27T00:00:06"), event("e5", "2026-11-27T00:00:05")]
PAGE_2 = [event("e4", "2026-11-27T00:00:04"), event("e3", "2026-11-27T00:00:03")]
PAGE_3 = [event("e2", "2026-11-27T00:00:02"), event("e1", "2026-11-27T00:00:01")]


def paged_upstream(requests: list[httpx.Request]) -> Handler:
    """Serve PAGE_1..3 keyed on the ``page_id`` upstream was given."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page_id = request.url.params.get("page_id")
        if page_id is None:
            return page(PAGE_1, "p2")
        if page_id == "p2":
            return page(PAGE_2, "p3")
        if page_id == "p3":
            return page(PAGE_3, None)
        raise AssertionError(f"unexpected page_id {page_id!r}")

    return handler


async def test_default_read_is_newest_first(environment: Environment) -> None:
    """An unpaged read must show recent activity, not the opening events."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return page(PAGE_1, None)

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1")

    assert captured[0].url.params["sort_order"] == "TIMESTAMP_DESC"
    assert [entry.text for entry in logs.entries] == ["assistant: hello", "assistant: hello"]
    assert logs.sort_order is LogSortOrder.NEWEST_FIRST
    assert logs.entries[0].timestamp == "2026-11-27T00:00:06"


async def test_oldest_first_asks_upstream_for_ascending_order(environment: Environment) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return page(PAGE_3, None)

    logs = await adapter_for(handler, environment).get_task_logs(
        environment, "t1", sort_order=LogSortOrder.OLDEST_FIRST
    )

    assert captured[0].url.params["sort_order"] == "TIMESTAMP"
    assert logs.sort_order is LogSortOrder.OLDEST_FIRST


async def test_cursor_pages_backward_without_overlap(environment: Environment) -> None:
    requests: list[httpx.Request] = []
    adapter = adapter_for(paged_upstream(requests), environment)

    first = await adapter.get_task_logs(environment, "t1", limit=2)
    assert [entry.timestamp for entry in first.entries] == [
        "2026-11-27T00:00:06",
        "2026-11-27T00:00:05",
    ]
    assert first.has_more is True
    assert first.next_cursor is not None

    second = await adapter.get_task_logs(environment, "t1", limit=2, cursor=first.next_cursor)
    assert [entry.timestamp for entry in second.entries] == [
        "2026-11-27T00:00:04",
        "2026-11-27T00:00:03",
    ]
    assert requests[-1].url.params["page_id"] == "p2"

    third = await adapter.get_task_logs(environment, "t1", limit=2, cursor=second.next_cursor)
    assert [entry.timestamp for entry in third.entries] == [
        "2026-11-27T00:00:02",
        "2026-11-27T00:00:01",
    ]
    assert third.has_more is False
    assert third.next_cursor is None

    seen = [entry.timestamp for logs in (first, second, third) for entry in logs.entries]
    assert len(seen) == len(set(seen)) == 6


async def test_cursor_continues_an_oldest_first_traversal(environment: Environment) -> None:
    """Omitting ``sort_order`` on a continuation must not flip the direction.

    A caller follows the documented advice ("pass next_cursor back as cursor")
    and leaves ``sort_order`` out. If that defaulted to newest-first, page two of
    an oldest-first traversal would silently reverse and overlap page one.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("page_id") is None:
            return page(PAGE_3[:2], "p2")
        return page(PAGE_3[2:], None)

    adapter = adapter_for(handler, environment)
    first = await adapter.get_task_logs(
        environment, "t1", limit=2, sort_order=LogSortOrder.OLDEST_FIRST
    )
    assert first.sort_order is LogSortOrder.OLDEST_FIRST

    second = await adapter.get_task_logs(environment, "t1", limit=2, cursor=first.next_cursor)
    assert second.sort_order is LogSortOrder.OLDEST_FIRST
    assert requests[-1].url.params["sort_order"] == "TIMESTAMP"


async def test_cursor_from_another_task_is_rejected(environment: Environment) -> None:
    """Upstream would answer page 1 with 200; the bridge must refuse instead."""
    requests: list[httpx.Request] = []
    adapter = adapter_for(paged_upstream(requests), environment)

    first = await adapter.get_task_logs(environment, "t1", limit=2)
    assert first.next_cursor is not None

    with pytest.raises(InvalidInputError, match="different task or environment"):
        await adapter.get_task_logs(environment, "t2", limit=2, cursor=first.next_cursor)


async def test_cursor_direction_mismatch_is_rejected(environment: Environment) -> None:
    requests: list[httpx.Request] = []
    adapter = adapter_for(paged_upstream(requests), environment)

    first = await adapter.get_task_logs(environment, "t1", limit=2)

    with pytest.raises(InvalidInputError, match="ordering"):
        await adapter.get_task_logs(
            environment,
            "t1",
            limit=2,
            cursor=first.next_cursor,
            sort_order=LogSortOrder.OLDEST_FIRST,
        )


@pytest.mark.parametrize("bad", ["", "not-base64!!", base64.urlsafe_b64encode(b"[]").decode()])
async def test_malformed_cursor_is_rejected(environment: Environment, bad: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no upstream call should happen for an invalid cursor")

    with pytest.raises(InvalidInputError):
        await adapter_for(handler, environment).get_task_logs(environment, "t1", cursor=bad)


async def test_cursor_is_opaque_and_not_the_raw_page_id(environment: Environment) -> None:
    """The upstream token is a server detail; callers must not see or forge it."""
    requests: list[httpx.Request] = []
    adapter = adapter_for(paged_upstream(requests), environment)

    first = await adapter.get_task_logs(environment, "t1", limit=2)
    assert first.next_cursor is not None
    assert "p2" not in first.next_cursor

    decoded = json.loads(base64.urlsafe_b64decode(first.next_cursor + "=="))
    assert decoded["p"] == "p2"
    assert decoded["v"] == 1
    assert decoded["t"] == "t1"


async def test_cursor_carries_no_secret_or_free_text(environment: Environment) -> None:
    """A cursor crosses the MCP boundary, so it must hold ids and a timestamp only.

    It is base64, not encrypted: anything put in it is readable by the caller.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return page(
            [event("e2", "2026-11-27T00:00:02", text="using secret-token now")],
            "p2",
        )

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1", limit=1)
    assert logs.next_cursor is not None

    raw = base64.urlsafe_b64decode(logs.next_cursor + "=" * (-len(logs.next_cursor) % 4))
    payload = json.loads(raw)

    assert set(payload) == {"v", "t", "e", "o", "p", "b"}
    assert "secret-token" not in logs.next_cursor
    assert "using" not in logs.next_cursor


async def test_out_of_order_event_does_not_stop_the_traversal(environment: Environment) -> None:
    """Upstream ordering is not perfectly monotonic, and the guard must allow it.

    Page 1's *last* item is 18:00:03 while an earlier item is 18:00:01. A boundary
    taken as the oldest timestamp (18:00:01) would demand the next page start
    strictly below it — but a legitimate next page starts at 18:00:02, so the
    traversal would stop early and silently hide the rest of the history.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("page_id") is None:
            return page(
                [
                    event("e4", "2026-11-27T18:00:05"),
                    event("e3", "2026-11-27T18:00:01"),
                    event("e2", "2026-11-27T18:00:03"),
                ],
                "p2",
            )
        return page([event("e1", "2026-11-27T18:00:02")], None)

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1", limit=4)

    assert [entry.timestamp for entry in logs.entries] == [
        "2026-11-27T18:00:05",
        "2026-11-27T18:00:01",
        "2026-11-27T18:00:03",
        "2026-11-27T18:00:02",
    ]
    assert len(requests) == 2
    assert logs.has_more is False


async def test_out_of_order_event_in_an_oldest_first_page_does_not_stop_it(
    environment: Environment,
) -> None:
    """The mirror case: a boundary from the newest timestamp cuts an ASC walk.

    Page 1's last item is 18:00:03 while an earlier item is 18:00:05. Bounding on
    the newest (18:00:05) would demand the next page start strictly above it, but
    the legitimate next page starts at 18:00:04.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("page_id") is None:
            return page(
                [
                    event("e1", "2026-11-27T18:00:01"),
                    event("e2", "2026-11-27T18:00:05"),
                    event("e3", "2026-11-27T18:00:03"),
                ],
                "p2",
            )
        return page([event("e4", "2026-11-27T18:00:04")], None)

    logs = await adapter_for(handler, environment).get_task_logs(
        environment, "t1", limit=4, sort_order=LogSortOrder.OLDEST_FIRST
    )

    assert [entry.timestamp for entry in logs.entries] == [
        "2026-11-27T18:00:01",
        "2026-11-27T18:00:05",
        "2026-11-27T18:00:03",
        "2026-11-27T18:00:04",
    ]
    assert len(requests) == 2
    assert logs.has_more is False


async def test_unrenderable_page_does_not_claim_more_history(environment: Environment) -> None:
    """The overlap guard must read the raw page, not the rendered entries.

    A page whose events all render to nothing used to leave the guard with
    nothing to compare, so it skipped the restart check, looped to the page cap,
    and handed back a cursor — telling the caller more history exists when the
    server was only ever repeating one page. Reading the raw page detects the
    restart even when no entry renders.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Page 1 again, whatever page_id was asked for: upstream ignored it.
        return page(
            [
                {
                    "id": "u1",
                    "timestamp": "2026-11-27T00:00:09",
                    "kind": "MessageEvent",
                    "llm_message": {"role": "assistant"},
                },
                {
                    "id": "u2",
                    "timestamp": "2026-11-27T00:00:02",
                    "kind": "MessageEvent",
                    "llm_message": {"role": "assistant"},
                },
            ],
            "p2",
        )

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1", limit=3)

    assert logs.entries == ()
    assert logs.next_cursor is None
    assert logs.has_more is False


async def test_stale_cursor_does_not_repeat_a_page(environment: Environment) -> None:
    """Upstream silently restarts at page 1 for a stale cursor.

    If it did, a naive client would return the same entries forever. The bridge
    carries the previous page's boundary in the cursor and stops when a page has
    not advanced past it.
    """
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_id = request.url.params.get("page_id")
        calls.append(page_id)
        # Both the first page and the "next" page return PAGE_1: upstream
        # ignored the cursor and restarted, which it does for a stale token.
        return page(PAGE_1, "p2")

    adapter = adapter_for(handler, environment)
    first = await adapter.get_task_logs(environment, "t1", limit=2)
    assert first.next_cursor is not None

    second = await adapter.get_task_logs(environment, "t1", limit=2, cursor=first.next_cursor)
    # It must not hand back the same events as if they were the next page.
    assert second.entries == ()
    assert second.has_more is False
    assert len(calls) == 2


async def test_upstream_requests_are_bounded(environment: Environment) -> None:
    """One call must not walk the whole history, however many pages exist."""
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("page_id"))
        # Each page is strictly older than the last, so the stale-page guard
        # never fires and only the page cap can end the loop. One entry per page
        # also keeps ``limit`` from being reached.
        index = len(calls)
        return page([event(f"e{index}", f"2026-11-27T00:00:{60 - index:02d}")], "always-more")

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1", limit=5)

    assert len(calls) == MAX_LOG_PAGES_PER_CALL
    assert len(logs.entries) == MAX_LOG_PAGES_PER_CALL
    # Stopping at the cap is a pause, not the end: the caller must be able to
    # continue rather than be told the history finished.
    assert logs.has_more is True
    assert logs.next_cursor is not None


async def test_page_cap_stops_the_loop(environment: Environment) -> None:
    """A page that yields no entries must not be re-requested forever."""
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("page_id"))
        index = len(calls)
        # Events that summarize to nothing, but with advancing timestamps so the
        # stale-page guard passes and only the cap can stop the loop.
        empty: dict[str, object] = {
            "id": f"x{index}",
            "timestamp": f"2026-11-27T00:00:{60 - index:02d}",
            "kind": "MessageEvent",
            "llm_message": {"role": "assistant"},
        }
        return page([empty], "always-more")

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1", limit=5)

    assert logs.entries == ()
    assert len(calls) == MAX_LOG_PAGES_PER_CALL


async def test_empty_history_is_reported_plainly(environment: Environment) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "next_page_id": None})

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1")

    assert logs.entries == ()
    assert logs.has_more is False
    assert logs.next_cursor is None
    assert logs.truncated is False


async def test_paginated_entries_are_still_redacted(environment: Environment) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return page([event("e1", "2026-11-27T00:00:01", text="using secret-token now")], None)

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1")
    assert "secret-token" not in logs.entries[0].text
