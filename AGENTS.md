# AGENTS.md

Instructions for any agent (human or AI) working in this repository.

## What this project is

OpenHands Connect is a local MCP server that bridges MCP clients to **one
explicitly configured** OpenHands Agent Server. It reports connectivity, submits
tasks, and exposes task status/logs/messages/stop. It is not an agent framework,
not a scheduler, and not a proxy that fans out to many environments.

## Non-negotiables

1. **Verify upstream, never invent it.** Every endpoint, header, and payload
   field used in `adapters/openhands.py` must be verified against a running
   Agent Server or its OpenAPI document. Record what was verified in
   `docs/adr/0002-openhands-agent-server-api.md`. If you cannot verify a
   capability, report it as unsupported rather than guessing.
2. **Never fake a capability.** If upstream has no terminate endpoint, do not
   report `cancelled=True`. `stop_task` pauses and says so.
3. **Credentials never touch disk, logs, or tool output.** Configuration names
   an environment variable (`credential_env_var`); the value is read at call
   time. `redaction.py` scrubs the MCP boundary as defence in depth — keep that
   wiring intact and add a test with a fail-before/pass-after check for any new
   outbound text field.
4. **Explicit environment selection.** No implicit fallback between local and
   cloud. Execution calls must name an environment ID.
5. **Policy before provider.** `PolicyEngine` decides before the adapter runs.
   High-impact operations are denied by default.
6. **Do not add push/PR GitHub Actions triggers.** Hosted CI is manual
   (`workflow_dispatch`) by maintainer decision; local validation is the gate.

## Commands

```bash
./scripts/validate.sh          # ruff + format + mypy + pytest (no network)
uv run pytest -m integration   # opt-in; needs OH_CONNECT_INTEGRATION_* env
```

`./scripts/validate.sh` must pass before any commit. It is the same gate a
reviewer will run.

## Layout

```text
src/openhands_connect/
  config.py              YAML config loading + validation
  domain/                provider-neutral models, errors, provider Protocol, registry
  adapters/openhands.py  the ONLY place that knows upstream HTTP shapes
  policy.py              allow/deny per environment + operation
  service.py             ConnectService: the use-case layer
  redaction.py           secret scrubbing
  audit.py               audit sinks
  mcp_server/            thin tool handlers + server wiring
  tests/                 fakes + unit tests + opt-in live tests
```

Keep MCP types out of `domain/`. Keep upstream specifics out of everything
except `adapters/openhands.py`.

## Style

- `ruff` (line length 100) and `mypy --strict` both gate the build. Production
  modules are fully annotated; test bodies relax `no-untyped-def` only.
- Comments explain *why* something is non-obvious. Do not narrate the diff.
- Prefer a small focused change over a broad refactor.

## Testing notes

- Core tests use `FakeProvider` and `httpx.MockTransport`; they must never
  require a live server.
- Live tests are marked `integration` and skip without
  `OH_CONNECT_INTEGRATION_BASE_URL`.
- When fixing a real bug, add a test that fails before the fix. State that you
  verified the failure, or it is not evidence.
- A local Agent Server authenticates with the `X-Session-API-Key` header, not
  `Authorization: Bearer`.
- `agent_profile_id` must be a UUID. The adapter resolves a configured profile
  *name* to its UUID via `GET /api/agent-profiles`.
