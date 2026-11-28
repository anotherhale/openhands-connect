# Reliability and failure semantics

How the bridge behaves when the Agent Server, the MCP client, or a task's
lifecycle misbehaves. Every claim about upstream below was probed against a
running **OpenHands Agent Server 1.60.1**; the probe results are recorded so a
later change can be checked against them rather than against memory.

The governing rule is the one in `AGENTS.md`: verify upstream, never invent it,
and never report a capability upstream does not have. Concretely, when the
bridge cannot tell what happened, it says so — it does not pick the convenient
answer.

## 1. Timeouts

Every request goes through one helper (`OpenHandsAdapter._send`) that applies
`environment.request_timeout_seconds` (default 30s, bounded to 600s by config
validation). A timeout never escapes as a raw `httpx` exception: it becomes
`ProviderTimeout` (a read) or `ProviderAmbiguousWrite` (a write that may have
been applied), both of which carry a redacted message.

`httpx`'s own exception text can name the host, the URL, or a header value, so
only the exception *type* and fixed phrases reach a message.

## 2. Bounded, retry-safe retries

`environment.max_attempts` (default 3, range 1–10; `1` disables retries) bounds
every retryable call. The bound is a hard cap, not a suggestion: a permanently
failing read stops after exactly `max_attempts` requests.

### What is retried

| Condition | Retried? | Why |
|---|---|---|
| Read (GET) + timeout / connect failure | Yes | Repeating a read has no effect on server state. |
| Read (GET) + 429 / 502 / 503 / 504 | Yes | Transient: capacity or gateway/overload. |
| Read (GET) + any other 4xx, or 500 | No | 4xx will be rejected again; a plain 500 means the server is broken and retrying only adds load. |
| Idempotent write (create, run, interrupt) + above | Yes | See "retry safety" below — the effect is keyed, so a repeat cannot duplicate it. |
| Non-idempotent write (message save) + transport loss or 5xx | **No, ever** | A repeat would store the message twice, and the outcome of the first is unknown. Reported as ambiguous. |
| Non-idempotent write (message save) + 4xx | No | The server rejected it before applying anything, so it is a plain failure. |

Backoff is capped exponential (`0.25s × 2ⁿ`, capped at 4s) and honours
`Retry-After` in seconds, clamped to the same 4s ceiling so a server asking for a
long wait cannot stall a tool call — the caller sees the 429 instead. The
HTTP-date form of `Retry-After` is ignored (it would need a clock comparison);
the bounded backoff takes over.

### Retry safety — why each write is or is not repeatable

These were verified, not assumed:

- **Create is idempotent when the caller supplies `conversation_id`.** Repeating
  `POST /api/conversations` with the same id returned `201` then `200` for the
  same conversation, with **no duplicate initial message** and **no re-run of a
  finished task** (one user message before and after; status stayed `finished`).
  The adapter therefore generates a UUID and sends it, which is what makes a
  create retry safe. The server advertises `idempotent_conversation_create_v1`.
- **`POST /run` is safe to repeat.** While running it answers `409 "Conversation
  already running"`, which for our purpose means *a run is in progress* — i.e.
  started. The adapter treats 409 as `started=True`.
- **`POST /interrupt` is idempotent.** Repeated calls returned `200`.
- **The message save is not idempotent.** No idempotency key exists, so it is
  attempted exactly once.

## 3. Ambiguous writes

A non-idempotent write that fails *after the request may have reached the
server* — read timeout, write timeout, reset connection, protocol error, or a 5xx
response — leaves the outcome undetermined. Reporting a plain failure would
invite a retry that duplicates the message; reporting success would be a lie. The
adapter raises `ProviderAmbiguousWrite` instead, which:

- is never retryable (`retryable=False`),
- carries the `task_id` it was keyed on, so the caller can check that task before
  retrying, and
- says so in the message ("it may or may not have been applied … check that id
  before retrying").

An ambiguous outcome is also recorded as `AuditOutcome.UNKNOWN` in the audit
trail, never `FAILED`: writing `failed` would assert the write did not land when
the bridge cannot know that, and an operator reading the trail would draw the
wrong conclusion. A genuine rejection (4xx, or a connect failure that proves
nothing was sent) is still `FAILED`.

A **connect** failure is different: it proves the request never left, so it is an
ordinary `ProviderUnavailable`, not an ambiguous one.

## 4. Not-ready server states

`GET /ready` answers `200` only after initialization and `503` while starting.
`HealthStatus` keeps `reachable` and `ready` separate, so a server that is up but
still initializing is reported as `reachable=true, ready=false` with an
"initializing; retry shortly" detail — never as ready to accept work, and never
as unreachable.

- `ready` is `None` when the route is absent (older server) or answers an
  unexpected status: readiness is then simply not claimed, never assumed.
- A starting server does **not** set `credential_state` to rejected; credential
  state stays whatever the authenticated probe returned.

## 5. Task-lifecycle semantics

- **Malformed vs unknown task id.** A non-UUID id makes the path parameter fail
  parsing (a 422 naming `path.conversation_id`); a well-formed unknown id returns
  `404`. These are kept distinct: the path-parse 422 becomes `InvalidInputError`
  ("upstream requires a UUID conversation id"), the 404 becomes
  `ProviderNotFound`. Conflating them sends a caller hunting for a deleted task
  when the id was simply malformed. A 422 about the *body* (e.g. an empty message)
  stays a plain `ProviderInvalidRequest` — the check reads the validation error's
  location, so a bad body is never mislabelled as a bad task id.
- **Message to a finished or running conversation** returns `200`; a finished
  conversation moves back to `running`. So "message accepted" never implies
  "task was idle".
- **Sending a message is two operations.** The adapter saves the message with
  `run=False`, then calls `POST /run` separately. This split was verified to
  behave identically to `run=True` when the run succeeds, and it is the only way
  to tell apart the two failure modes that a single request collapses:
  - save rejected → `ProviderInvalidRequest`/`ProviderAmbiguousWrite`, nothing
    stored;
  - save accepted, run refused (`429` "run capacity full") → the message **is**
    stored and only the run did not start.

  `MessageDelivery` reports `saved` and `started` separately; the tool layer's
  `sent` is true only when the run actually started, so a stored-but-idle message
  never reads as delivered.
- **`started` has three values, not two.** `True` = run started (or already
  running: `409`), `False` = run definitively refused (`429` capacity full), and
  `None` = the run request failed in a way that cannot be shown either way (5xx,
  timeout, lost response). `None` never raises: once the message is saved, the
  fact that it is stored must survive a failing run step, or the caller would
  resend the whole message and store it twice. `sent` is `false` for both `False`
  and `None`.
- **`stop_task` does not cancel.** Upstream has no terminate endpoint; `interrupt`
  and `pause` both leave the conversation `paused` and resumable. The result
  keeps `cancelled=false` and states that plainly.

## 6. Shutdown

`build_server` installs an MCP lifespan whose `finally` calls
`service.provider.aclose()`, so a client disconnect or a stop signal releases
provider resources instead of dropping them mid-flight.

## 7. Configuration added for this issue

```yaml
environments:
  - id: local
    # ...
    request_timeout_seconds: 30   # per request, 0 < t <= 600
    max_attempts: 3               # 1 disables retries; 1..10
```

## Testing

`src/openhands_connect/tests/test_reliability.py` covers each behaviour above on
`httpx.MockTransport`, with no live server. The suite-wide `_no_retry_sleep`
fixture zeroes only the backoff *delay* constants, so retry tests exercise the
real retry decisions (attempt counts, which statuses retry) without sleeping.

Each new behaviour was mutation-checked: reverting it makes the corresponding
test fail.

Beyond the happy paths, the tests pin the honest-failure edges:

- an ambiguous write is audited `UNKNOWN`, and a definite rejection is still
  `FAILED` (both directions checked);
- a saved message whose run step fails returns `started=None` and saves the
  message exactly once — never a raised error that would invite a resend;
- a 422 whose body is not FastAPI's usual shape (non-JSON, `detail` not a list,
  `loc` missing or not a list, a top-level list) degrades to
  `ProviderInvalidRequest` without mislabelling the task id;
- a non-JSON or non-object 200, or one with no conversation id, is a
  `ProviderProtocolError`, never a silently empty task.

## Not done here

- **Log paging.** `get_task_logs` still returns the earliest events, not the
  newest (upstream sorts ascending and pages forward). Tracked separately.
- **`waiting_for_confirmation`.** Mapped from the OpenAPI enum, never observed
  live.
- **Live 429 / ambiguous-write coverage.** Reproduced synthetically; not driven
  against a live server that can be made to fail on demand.
