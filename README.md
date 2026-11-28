# OpenHands Connect

**A universal bridge connecting ChatGPT and AI assistants to local, self-hosted, and cloud OpenHands agent environments through MCP. Extensible, secure, and customizable.**

OpenHands Connect bridges conversational AI clients to OpenHands execution environments. It aims to let users explicitly select an environment, dispatch work, observe progress, and retrieve results without coupling the client to runtime-specific APIs.

> **Status:** v0.1 vertical slice implemented and tested against OpenHands Agent
> Server 1.60.1. Local-only: one explicitly configured environment per call.
> Cloud/relay, multi-provider, and UI are not implemented.

## What works today

A local MCP server that connects to **one explicitly configured** OpenHands
Agent Server, reports connectivity, submits a task, and exposes task status,
bounded logs, follow-up messages, and stop.

| MCP tool | Effect |
|---|---|
| `list_environments` | List configured environments (sanitized, no credentials) |
| `check_environment` | Reachability **and** credential check for one environment ID (reports `credential_state`) |
| `create_task` | Submit a prompt; returns a task ID |
| `get_task` | Normalized status |
| `get_task_logs` | Bounded, redacted log entries (max 100) |
| `send_task_message` | Follow-up instruction to a running or paused task |
| `stop_task` | Interrupt/pause — see *Limitations* |
| `get_task_result` | Completion summary when available |

Task execution **modifies files and runs commands** in the selected
environment's workspace. Environment selection is explicit; there is no implicit
local/cloud fallback.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- An OpenHands Agent Server (for real use). Everything except the opt-in
  integration tests runs without one.

## Install

```bash
git clone <this repo> && cd openhands-connect
uv sync --extra dev
```

## Configure

Copy the example and edit it:

```bash
cp examples/environments.example.yaml config/environments.yaml
```

```yaml
schema_version: 1
environments:
  - id: local
    name: Local Agent Server
    provider: openhands
    base_url: http://127.0.0.1:18000
    enabled: true
    request_timeout_seconds: 30
    max_attempts: 3                    # 1 disables retries
    default_agent_profile_id: autonomous   # a profile NAME or UUID
    allowed_operations:
      - check_environment
      - create_task
      - get_task
      - get_task_logs
      - get_task_result
      - send_task_message
      - stop_task
```

Notes:

- **Credentials are never in this file.** An environment names the variable that
  holds its token (`credential_env_var: OH_STAGING_KEY`); the value is read at
  call time and never logged.
- **Operations are opt-in.** An environment that omits `allowed_operations`
  permits nothing beyond being listed.
- `default_agent_profile_id` accepts a profile **name** (`autonomous`) or a
  **UUID**. A name is resolved through the server; an unknown name is reported
  with the list of available profiles rather than a bare 422.

Config is read from `OH_CONNECT_CONFIG` if set, else `config/environments.yaml`.

## Run

```bash
uv run openhands-connect
```

Audit events go to stderr; set `OH_CONNECT_AUDIT_LOG=/path/to/audit.log` to
append to a file instead. Audit records environment, operation, task ID,
timestamp, and outcome — not prompt or output text.

### Register with an MCP client

```json
{
  "mcpServers": {
    "openhands-connect": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/openhands-connect", "openhands-connect"]
    }
  }
}
```

## Validate

```bash
./scripts/validate.sh        # ruff + format + mypy + pytest; no network
```

### Opt-in integration tests

Skipped unless a server is configured:

```bash
OH_CONNECT_INTEGRATION_BASE_URL=http://127.0.0.1:18000 \
OH_CONNECT_INTEGRATION_API_KEY="$SESSION_KEY" \
./scripts/validate.sh --with-integration
```

A local Agent Server authenticates with the `X-Session-API-Key` header, not
`Authorization: Bearer`.

## Limitations

- **No conversation terminate endpoint exists upstream.** `stop_task` uses
  `interrupt`, which pauses; it reports `cancelled=False` rather than claiming a
  cancellation the server cannot perform. A paused task stays resumable —
  `send_task_message` continues the same conversation.
- **`get_task_logs` returns at most 100 entries.** Upstream rejects a larger
  `limit` with a 422. Larger values are clamped, not rejected.
- **Logs are the oldest events in the conversation, not the newest.** The
  upstream `events/search` route pages forward from the beginning, so a long run
  shows its opening events rather than its tail.
- Local server support only; cloud/relay is deferred.
- Single provider (`openhands`). The adapter boundary exists so others can be
  added.
- **Agent profiles are referenced by name or ID, but cannot be listed.** There is
  no `list_agent_profiles` tool in v0.1: discovering a profile name is a
  configuration step done against the server directly. Adding one is tracked for
  Issue #6 and deliberately not part of this surface yet.
- **An earlier hang is not explained.** Some conversations previously hung
  instead of replying to a PING. Controlled runs during validation completed
  normally, but the cause was not identified and the symptom may recur — see
  [integration validation](docs/INTEGRATION-VALIDATION.md#not-verified).
- Verified against Agent Server **1.60.1**; see
  [ADR 0002](docs/adr/0002-openhands-agent-server-api.md) and
  [integration validation](docs/INTEGRATION-VALIDATION.md) for exactly what was
  exercised live.


## Goals
- Connect to local, self-hosted, and cloud OpenHands instances.
- Provide unified environment discovery, health checks, task creation, status, logs, messaging, and cancellation.
- Expose functionality through Model Context Protocol (MCP).
- Keep OpenHands-specific details behind an adapter so other execution providers may be added.
- Support configuration and customization.
- Apply policy checks and auditable task lifecycle events.

## MVP non-goals
- Reimplement OpenHands or build a general agent framework.
- Automatic PR merge, deployment, or irreversible actions.
- Unauthenticated public exposure of OpenHands APIs.
- Premature distributed services or multi-agent orchestration.

## Target capabilities
| Capability | Purpose |
|---|---|
| Environment Registry | Named local, self-hosted, and cloud targets |
| Environment Health | Connectivity diagnostics without credential leakage |
| Task Dispatch | Submit bounded work to an explicitly selected environment |
| Task Observation | Status, logs, and safe outputs |
| Task Messaging | Send follow-up instructions |
| Task Control | Cancel/stop tasks |
| Policy & Approval | Constraints and approval gates |
| Audit | Record lifecycle and control events |
| Provider Adapter | Encapsulate OpenHands API specifics |

## Architecture overview
```text
AI Client (ChatGPT / MCP client)
              |
              v
         MCP Interface
              |
              v
    Connect Core / Policy Engine
       |        |        |
       v        v        v
 Environment  Task     Audit
 Registry    Manager   Events
              |
              v
    Agent Execution Provider
              |
              v
       OpenHands Adapter(s)
         /            \
        v              v
  Local/Private       Cloud
  OpenHands          OpenHands
```

## MVP roadmap
1. ~~Named environment configuration.~~ Done
2. ~~Provider contract and OpenHands adapter.~~ Done
3. ~~Environment listing and health tools.~~ Done
4. ~~Task create/status/logs/message/stop.~~ Done
5. ~~Policy checks and audit events.~~ Done
6. ~~Mock-based tests and setup documentation.~~ Done

Next: cloud/relay connectivity, additional providers, and an MCP client UI.

## Security
No credentials in prompts, tool outputs, logs, or committed files. Environment selection is explicit; local and cloud permissions remain separate. Remote connectivity must use authenticated secure transport or relay. Higher-impact operations require explicit policy and approval.

See [Project Charter](docs/PROJECT_CHARTER.md), [Architecture](docs/ARCHITECTURE.md), [Roadmap](docs/ROADMAP.md), [Security](docs/SECURITY.md), [Contributing](CONTRIBUTING.md), [ADR 0001](docs/adr/0001-mcp-as-client-interface.md), and [ADR 0002](docs/adr/0002-openhands-agent-server-api.md).

## Development

`AGENTS.md` has the working rules for this repository. In short: verify upstream
endpoints before using them, never report a capability the server does not have,
keep credentials out of files and logs, and run `./scripts/validate.sh` before
committing.

## License
MIT. See [LICENSE](LICENSE).
