# Validated against a real local Agent Server

This document records what was actually exercised against a live OpenHands
Agent Server, not what the code is expected to do. Everything below was run on
the machine described in *Tested setup*; anything not run is listed under
*Not verified*.

## Tested setup

| Item | Value |
|---|---|
| OpenHands Agent Server | `1.60.1` (`/server_info` reports the version) |
| Agent Server URL | `http://127.0.0.1:18000` |
| Automation backend | `127.0.0.1:18001` (not used by this bridge) |
| Auth | `X-Session-API-Key` header |
| Bridge | this repo, `uv run openhands-connect` (stdio MCP) |
| MCP client | `mcp` Python SDK `ClientSession` over `stdio_client` |
| Python | 3.13 (uv-managed) |

The Agent Server was already running and configured with an agent profile and
an LLM. This bridge does not start it.

## Environment configuration

No credential is written to disk. The config names the variable; the value is
read at call time.

```yaml
# config/environments.yaml  (gitignored; copy from examples/)
schema_version: 1
environments:
  - id: local
    name: Local Agent Server
    provider: openhands
    base_url: http://127.0.0.1:18000
    enabled: true
    request_timeout_seconds: 120
    default_agent_profile_id: autonomous
    credential_env_var: OPENHANDS_AUTOMATION_API_KEY
    allowed_operations:
      - check_environment
      - create_task
      - get_task
      - get_task_logs
      - get_task_result
      - send_task_message
      - stop_task
```

```bash
export OPENHANDS_AUTOMATION_API_KEY=...   # never committed, never echoed
export OH_CONNECT_CONFIG=/absolute/path/to/config/environments.yaml
```

Environment discovery and health:

```bash
uv run openhands-connect     # stdio; drive it with an MCP client
```

## MCP client used

The MCP Python SDK, not unit tests and not direct Python calls:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command="uv",
    args=["run", "--directory", "/path/to/openhands-connect", "openhands-connect"],
    env={
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "OH_CONNECT_CONFIG": "/path/to/config/environments.yaml",
        "OPENHANDS_AUTOMATION_API_KEY": os.environ["OPENHANDS_AUTOMATION_API_KEY"],
    },
)
async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()          # 8 tools
        await session.call_tool("check_environment", {"environment_id": "local"})
```

Verified on this path:

- `initialize` succeeds; the server reports `openhands-connect` `0.1.0`.
- All 8 tools are exposed: `list_environments`, `check_environment`,
  `create_task`, `get_task`, `get_task_logs`, `send_task_message`, `stop_task`,
  `get_task_result`.
- **stdout carries only JSON-RPC.** No log line, banner, or traceback is mixed
  into the protocol stream (the audit sink writes to stderr).
- **No credential appears on stderr** or in any tool result.
- Closing and re-opening the session works; the second session exposes the same
  8 tools and a working `check_environment`.

## Workflow verified end to end

Each step below returned the response shown, and the state transition was
confirmed by polling `get_task`, not inferred from an HTTP status.

| # | Tool | Observed |
|---|---|---|
| 1 | `list_environments` | `count: 1`, environment `local`, no `credential_env_var` in the payload |
| 2 | `check_environment` | `reachable: true`, `credential_state: "accepted"`, `server_version: "1.60.1"` |
| 3 | `create_task` | task id returned, state `dispatched` |
| 4 | (upstream runs it) | `get_task` moves `dispatched` → `running` → `completed` |
| 5 | `get_task` | `state: completed`, `provider_state: finished` |
| 6 | `get_task_logs` | bounded, redacted entries: prompt, state updates, final message |
| 7 | `send_task_message` | `sent: true` |
| 8 | follow-up received | `get_task_result` returns the second answer (`PONG2`), proving the agent processed it |
| 9 | `stop_task` (while running) | `state: paused`, `cancelled: false` |
| 10 | `get_task_result` | before resume: `available: false`, `detail: "no final response while task state is paused"` |

Resume after interrupt: `send_task_message` on the **paused** task moved it back
to `running` and then `completed`, and `get_task_result` returned the new answer
(`RESUMED`). Interruption therefore pauses a resumable conversation; it does not
terminate the agent, and the bridge says so rather than claiming cancellation.

`stop_task` on an already-completed task returns the completed state with
`cancelled: false` and a detail explaining that the server cannot terminate a
run — it does not pretend the stop did something.

## Workspace test

A real task was run through the bridge that:

1. created `report.txt` in the task workspace,
2. wrote the line `openhands-connect issue 5 workspace validation`,
3. ran `cat report.txt` to validate the content,
4. reported the command output and the absolute path as its final answer.

`get_task_result` returned the stdout line and the path
`/tmp/oh-connect-issue5/workspace/report.txt`; the file was checked on disk and
matched. The agent confirmed no files outside its working directory were
touched. The workspace was a throwaway directory, so the test could not reach
anything of value.

## Failure and security results

| Scenario | Result |
|---|---|
| Missing credential env var | Fails closed before sending: `connect_error`, "requires credential env var … which is not set". No request is made. |
| Invalid credential | `check_environment` → `reachable: false`, `credential_state: "rejected"`, "rejected the credential". Tools → `provider_unauthorized`, `retryable: false`. |
| Server unavailable (refused) | `check_environment` → `reachable: false`, `credential_state: "unknown"`. Tools → `provider_unavailable`, `retryable: true`. |
| Timeout | Tools → `provider_timeout`, `retryable: true`. `check_environment` → `reachable: false`. |
| Malformed task ID | `provider_invalid_request` with the upstream 422 message. |
| Unknown task ID | `provider_not_found`. |
| Unknown environment ID | `unknown_environment`, listing the configured IDs. |
| Interrupted task | `paused`, `cancelled: false`, resumable (above). |
| MCP client restart | Second session exposes the same 8 tools and a working health check. |
| Credential in output | No token in stdout, stderr, tool results, or audit records. |

Rate limiting (HTTP 429) is mapped to `provider_rate_limited` and is covered by a
unit test with a synthetic 429, but was **not** triggered against the live
server: nothing in this setup produced a 429. See *Not verified*.

Duplicate submission: the bridge performs no internal retry and no automatic
re-send, so a single tool call produces exactly one upstream request. Retrying is
the client's decision, and the `retryable` flag tells it whether that is safe.
A duplicate `send_task_message` is a genuine second message — the server does not
deduplicate — so callers must not retry it blindly.

## Bugs found and fixed during this validation

| Bug | Symptom | Fix |
|---|---|---|
| Health check ignored the credential | `check_environment` returned `reachable: true` with a wrong token, because `/server_info` is unauthenticated | When a credential is configured, a second request to an authenticated route (`/api/conversations/count`) confirms it, and the result is reported as a structured `credential_state` |
| Transport errors escaped the taxonomy | A refused connection or timeout surfaced as `internal_error` with no cause and no retry hint | All requests go through a helper that maps `httpx` failures to `provider_unavailable` / `provider_timeout` |
| Log limit exceeded the upstream maximum | `get_task_logs(limit=200)` — the advertised maximum — always failed with a 422 | The bound is 100, the value upstream accepts, in both the adapter and the tool schema |
| Credentials in repr form were not redacted | `{'token': 'x'}` (the shape upstream events use) leaked verbatim into `get_task_logs` | A secret-assignment pattern covers single-quoted and bare `key=value` forms |
| `stop_task` description was ambiguous | It did not say the paused task stays resumable | The description now states that `send_task_message` resumes it |

Each fix has a regression test that was confirmed to fail before the fix.

### Credential probe contract

The health fix leans on one authenticated route, so its contract was checked
rather than assumed. Against Agent Server 1.60.1:

| Request | Result |
|---|---|
| `GET /api/conversations/count` with a valid key | `200`, body is a bare integer (e.g. `783`) |
| Same with an invalid or absent key | `401`, `{"detail":"Unauthorized"}` |
| `GET /server_info` with an invalid key | `200` — it does not enforce auth, which is why it cannot be the only check |

The route is declared in the server's own OpenAPI document
(`GET /api/conversations/count`, "Count Conversations", `security:
[{"APIKeyHeader": []}]`) — the same `APIKeyHeader` scheme every conversation
route uses, not a special-purpose endpoint. A future release that renames or
removes it is a breaking change for this probe; the troubleshooting table below
covers the symptom.

Because only `401`/`403` mean "the token is wrong", `check_environment` reports a
structured `credential_state` so a broken *server* is never mistaken for a bad
*token*:

| `credential_state` | Meaning |
|---|---|
| `not_configured` | No credential is set for this environment; only `/server_info` was asked. |
| `accepted` | A credential is configured and the authenticated probe accepted it. |
| `rejected` | The probe answered `401`/`403`: the token is wrong or expired. |
| `unknown` | A credential is configured but the probe returned a 5xx, 429, timeout, or other unexpected status. Says nothing about the token. |

`unknown` never counts as `accepted`: `reachable` is `false` in that case, so a
health check cannot pass on an unverified credential.

## Not verified

- **The earlier hung PING/PONG conversations are *not* explained.** Before this
  work, some conversations hung instead of replying to a PING. The fresh
  controlled runs described above completed normally, but a clean run does not
  prove what caused the earlier hangs: no reproduction was attempted against
  the failing conditions, and no root cause was identified. Treat the hang as
  an **open, unexplained symptom** that may recur. This document reports only
  that the lifecycle worked in these runs — not that the hang is fixed.
- **HTTP 429 / capacity limits.** No live 429 was produced. The mapping is unit
  tested only.
- **`waiting_for_confirmation`.** The configured agent profile never entered
  this state, so it is mapped from the OpenAPI enum but not observed live.
- **Cloud or remote servers, other providers, multiple environments.** Out of
  scope for v0.1 and not exercised.
- **A full client-driven retry loop.** The `retryable` flag was checked; an
  actual retry-after-failure sequence was not driven through a client.
- **Log paging.** `get_task_logs` returns the earliest events for a task, not the
  newest: upstream `events/search` sorts ascending and pages forward with
  `next_page_id`, which the bridge does not yet follow. Deferred to Issue #6.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `connect_error` … "credential env var … not set" | The variable named by `credential_env_var` is not exported in the server's environment. |
| `reachable: false`, `credential_state: "rejected"` … "rejected the credential" | The token is wrong or expired. Re-read it from the running Agent Server. |
| `reachable: false`, `credential_state: "unknown"` | The server is up but the credential probe could not complete (5xx, 429, or timeout). This is a *server* problem, not a bad token — do not rotate the credential yet. |
| `provider_unavailable` … "unreachable" | Nothing is listening on `base_url`. Start the Agent Server or fix the URL. |
| `provider_timeout` | The server is slow or wedged. Raise `request_timeout_seconds`, or investigate the server. |
| `provider_invalid_request` … "valid UUID" | Task IDs are UUIDs. A non-UUID string is reported as an invalid-input error; a well-formed unknown id is `provider_not_found`. |
| `provider_ambiguous_write` … "may or may not have been applied" | A mutating request (message save) failed after it may have reached the server. It is **not** retryable — check the named task before retrying, or the message may be duplicated. |
| `reachable: true`, `ready: false` | The server is up but still initializing. Retry shortly; it is not ready to accept work yet. |
| `provider_not_found` | The task ID does not exist on that environment (or was deleted). |
| `unknown_environment` | The `environment_id` is not in the config; the error lists the valid ones. |
| `check_environment` says `reachable: true` but calls fail with 401 | Should not happen after the fix; if it does, the credential probe route changed upstream. |

## Carried forward to Issue #6

Concrete follow-ups from this validation, not addressed here:

1. **Log paging.** Follow `next_page_id` from `events/search` with bounded page
   sizes so callers can reach the newest events, not just the opening ones.
2. **`send_task_message` idempotency.** A blind client retry currently sends a
   second message. Either add an idempotency key or document a strict
   caller-supplied request-identity contract.
3. **Live 429 / `waiting_for_confirmation` coverage.** Add opt-in live tests when
   the environment can reproduce them; keep the synthetic unit coverage until
   then.
4. **Profile-name friction.** Consider a `list_agent_profiles` capability only if
   it fits the provider-neutral design without expanding the v0.1 surface
   (see the README limitation).
