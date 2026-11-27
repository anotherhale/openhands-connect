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
| GET | `/api/agent-profiles` | resolve a profile **name** to its UUID |
| POST | `/api/conversations` | create a task (conversation) |
| GET | `/api/conversations/{id}` | task status |
| GET | `/api/conversations/{id}/events/search` | bounded log retrieval |
| GET | `/api/conversations/{id}/agent_final_response` | task result |
| POST | `/api/conversations/{id}/events` | follow-up message |
| POST | `/api/conversations/{id}/interrupt` | stop (see limitations) |

### The health check must make two requests

`/server_info` answers `200` with the version **even when the credential is
wrong** — it does not enforce auth. A health check that stops there reports a
broken token as healthy, which is what happened before this was found. When an
environment configures a credential, the adapter therefore follows up with an
authenticated route (`GET /api/conversations/count`, which returns `401` for a
bad key) and reports `reachable=false` when it is rejected. With no credential
configured, only `/server_info` is asked, so an anonymous server is not called
broken.

### `events/search` caps `limit` at 100

A `limit` above `100` fails with `422` ("Input should be less than or equal to
100"). The bridge bounds to 100 and advertises that maximum in the tool schema.
The route sorts `TIMESTAMP` (oldest first) by default and pages forward with
`next_page_id`; the bridge does not page, so a long run's logs are its opening
events.

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
