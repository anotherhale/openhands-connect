# ADR 0002 — OpenHands Agent Server API surface

- **Status:** Accepted
- **Date:** 2026-11-27
- **Context:** The v0.1 vertical slice must call a real OpenHands server without
  inventing endpoints.

## Decision

Target the **OpenHands Agent Server** (`agent-server` package, routes under
`/api`). Not the Sandbox Server control plane (`/api/v1`), and not the archived
V0 Local GUI API.

Everything below was read from the OpenAPI document of a running Agent Server
**1.60.1** (`GET /openapi.json`, 149 paths) and exercised end to end by
`tests/test_live_server.py` and `tests/test_mcp_live.py`.

## Authentication

| Item | Value |
|---|---|
| Header | `X-Session-API-Key: <token>` |
| Not | `Authorization: Bearer <token>` — rejected with 401 |

A server bound to loopback with no session keys configured accepts
unauthenticated requests. The adapter sends the header only when the environment
names a credential variable.

## Endpoints used

| Method | Path | Purpose |
|---|---|---|
| GET | `/server_info` | health check; **unauthenticated**, returns `version` |
| GET | `/api/conversations/count` | credential probe for health (see below) |
| GET | `/ready` | readiness probe: `200` when initialized, `503` while starting |
| GET | `/api/agent-profiles` | resolve a profile **name** to its UUID |
| POST | `/api/conversations` | create a task (conversation), with caller-supplied `conversation_id` |
| GET | `/api/conversations/{id}` | task status |
| GET | `/api/conversations/{id}/events/search` | bounded log retrieval |
| GET | `/api/conversations/{id}/agent_final_response` | task result |
| POST | `/api/conversations/{id}/events` | follow-up message (sent with `run=False`) |
| POST | `/api/conversations/{id}/run` | start the run after a saved message |
| POST | `/api/conversations/{id}/interrupt` | stop (see limitations) |

### Idempotent creation and the two-step message send

Verified on 1.60.1 (see `docs/RELIABILITY.md` for the probes):

- **`POST /api/conversations` is idempotent when `conversation_id` is supplied.**
  Repeating it returned `201` then `200` for the same conversation, with no
  duplicate initial message and no re-run of a finished task. The adapter always
  generates the id, which is what makes a create retry safe.
- **`POST /run` answers `409` while already running**, which the adapter reads as
  "a run is in progress" (i.e. started), and **`POST /interrupt` is idempotent**
  (repeated `200`s).
- **The message save is not idempotent.** It is therefore attempted once, and the
  run is started by a separate `POST /run`. A single `run=True` request would
  collapse "not stored" and "stored but not running" into one status.
- **A malformed id returns `422` (`uuid_parsing`), an unknown one `404`.** These
  are reported as `InvalidInputError` and `ProviderNotFound` respectively.

### The health check must make two requests

`/server_info` answers `200` with the version **even when the credential is
wrong** — it does not enforce auth. A health check that stops there reports a
broken token as healthy, which is what happened before this was found. When an
environment configures a credential, the adapter therefore follows up with an
authenticated route (`GET /api/conversations/count`, which returns `401` for a
bad key) and reports `reachable=false` when it is rejected. With no credential
configured, only `/server_info` is asked, so an anonymous server is not called
broken.

**Probe contract, verified against 1.60.1 and its OpenAPI document:**

- `GET /api/conversations/count` is declared in the server's OpenAPI document
  ("Count Conversations") with `security: [{"APIKeyHeader": []}]` — the same
  key scheme as every conversation route, not a purpose-built health endpoint.
- Valid key → `200` with a bare integer body. Invalid or absent key → `401`
  `{"detail":"Unauthorized"}`.

Only `401`/`403` are treated as a bad credential. Any other non-success
(5xx, 429) or a transport failure is reported as `credential_state="unknown"`
with `reachable=false`: a broken server must not send a user to rotate a good
token, and an unverified credential must never read as healthy. This is the
`CredentialState` enum on `HealthStatus` (`not_configured` / `accepted` /
`rejected` / `unknown`), surfaced as a structured field by `check_environment`.

### `events/search` caps `limit` at 100

A `limit` above `100` fails with `422` ("Input should be less than or equal to
100"); `limit=0` fails with `422` ("greater than 0"). The bridge bounds to 100
and advertises that maximum in the tool schema.

### `events/search` pagination: what is actually supported (verified)

Probed against a live **Agent Server 1.60.1** on 2026-11-28 against a
conversation holding 612 events. Every claim below is an observation, not an
inference from a parameter name.

**`sort_order=TIMESTAMP_DESC` genuinely works.** `EventSortOrder` is
`{"TIMESTAMP", "TIMESTAMP_DESC"}`. With `TIMESTAMP_DESC` the first page starts at
the newest event and each page is strictly older than the last. `TIMESTAMP`
(oldest first) is the server default, which is why an unpaged read shows the
opening of a run. An invalid value is a `422` naming both accepted values.

**Backward paging via `next_page_id` is correct and lossless.** A full traversal
of the 612-event conversation at `limit=100` took 7 pages, returned 612 events,
and produced **612 unique ids — zero duplicates, zero skips**, matching
`GET /events/count` exactly. Consecutive pages did not overlap, and each page was
strictly older than the previous page's last entry.

**`next_page_id` is the id of the next page's first event.** Corrected in Issue
#14 (this section previously said it was opaque and unrelated to any event id —
that was an untested assumption). Verified on 1.60.1: requesting `page_id=<token>`
returns a page whose first item's `id` is byte-identical to the token, across
`limit ∈ {1,2,3,4,5,7}` × both sort orders (**192/192 pages**). So the token is an
*inclusive* identity anchor and self-validates: a page whose first id differs from
the token is not the page that was asked for.

**A bad cursor is silently ignored — the server restarts at page 1.** Probed with
three inputs: a non-UUID string, a well-formed but nonexistent UUID, and a cursor
belonging to a *different* conversation. All three returned `HTTP 200` with a
body byte-identical to page 1. There is **no error signal** for an invalid or
cross-task cursor. A client that blindly reuses a stale or foreign cursor will
loop over page 1 forever and never detect it. The bridge therefore owns cursor
validation: it binds each cursor to the task and to the sort direction, and
refuses a mismatch itself rather than trusting upstream to. This is also why the
first-page-equals-token case must be rejected separately: a restart at page 1 can
look like a valid page if the token happens to name page 1's own first item.

**A cursor is not bound to the sort order.** Reusing a `TIMESTAMP_DESC` cursor
with `sort_order=TIMESTAMP` returned `200` and 4 of 5 items overlapping the
descending page — the token encodes a position, not a direction. Mixing
directions mid-traversal silently corrupts a page. The bridge therefore pins the
direction per traversal and encodes it in the cursor.

**Timestamps are microsecond-resolution and were all distinct** across the 612
events (612 distinct values, no ties). Ordering is therefore deterministic on
`timestamp` alone in practice.

**Ordering is *not* monotonic across page boundaries, at any page size.** One
page of 100 held an event whose timestamp sat between its neighbours'
(`…537684` after `…590859` in a `TIMESTAMP_DESC` page), and the raw upstream page
showed the same inversion — it is the server's ordering, not the bridge's
rendering. At `limit` 1–4 the inversions cross page boundaries often enough that a
timestamp-directional guard aborts after a couple of pages (4/612 newest-first,
608/612 oldest-first). Three consequences, all load-bearing:

* The bridge **preserves upstream order** rather than re-sorting, so the returned
  order matches what a direct `GET` would show.
* The overlap/repeat guard is **item-id identity**, not a timestamp comparison.
  Comparing the requested token against the returned page's first id is exact and
  direction-agnostic. A timestamp guard calls a legitimate continuation a restart
  whenever the boundary inverts and truncates the traversal — the Issue #14
  defect. Covered by `test_out_of_order_event_*_does_not_stop_the_traversal` and
  the live small-limit sweep.
* Nothing about the boundary needs to travel in the cursor: the token is its own
  anchor.

A full traversal is lossless at every limit tested: 612 events, 612 unique ids,
zero duplicates, zero skips, both directions (see
`docs/INTEGRATION-VALIDATION.md`).

**Keyset alternative also works.** `timestamp__lt=<ts>` with
`sort_order=TIMESTAMP_DESC` returned exactly the same page as `page_id` and also
avoids overlap with the previous page. The bridge uses `next_page_id` (native,
server-owned) and keeps the boundary timestamp only for an overlap guard, so a
server that returned a stale page cannot duplicate entries silently.

**Empty history** could not be produced live: all 100 conversations reachable on
the test server had at least one event, so the empty-page shape is covered
synthetically against the documented `{"items": [], "next_page_id": null}`
envelope rather than by observation.

## Creating a conversation

Exactly one of `agent_profile_id`, `agent_settings`, or `agent` is required;
none yields `422`. Two details are easy to get wrong:

1. **`agent_profile_id` must be a UUID.** Posting a profile *name* fails with
   `422 uuid_parsing` — an error that never mentions profiles. Configuration
   files naturally hold a readable name, so the adapter resolves it through
   `GET /api/agent-profiles` and reports the available names when it cannot.
   A value that already parses as a UUID is sent as-is, with no lookup.
2. **`initial_message.run` must be `true`.** Without it the server stores the
   message and never starts the agent loop, so the task sits idle forever.

## Execution status

`ConversationExecutionStatus` enum, verified against the spec:

```text
idle | running | paused | waiting_for_confirmation | finished | error | stuck | deleting
```

Mapping to the bridge's normalized `TaskState` lives in `STATE_MAP` in
`adapters/openhands.py`.

## Limitations that must not be papered over

**There is no endpoint that terminates a conversation.** The spec has
`interrupt` and `pause`; both land the conversation in `paused`. `stop_task`
therefore reports `cancelled=False` and states the real effect. Claiming
`cancelled=True` would assert a capability upstream does not have.

**`deleting` maps to `UNKNOWN`, not `CANCELLED`.** Deletion is not cancellation:
a conversation being torn down was not necessarily stopped by us, and upstream
exposes no verified signal that says it was. Reporting `CANCELLED` would invent
that signal, exactly as claiming `cancelled=True` would. The raw
`provider_state` is preserved on the returned `Task`, so a caller that knows
better can interpret it.

## Consequences

- Upstream shapes are isolated in `adapters/openhands.py`; nothing else in the
  codebase knows these paths.
- A future server version that renames a route or changes the status enum should
  be caught by the opt-in integration tests, which run against a live server.
- The adapter maps transport errors, timeouts, and non-2xx responses into the
  provider error taxonomy in `domain/models.py` rather than leaking `httpx`
  exceptions.
