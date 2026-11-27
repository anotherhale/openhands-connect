# OpenHands Connect

**A universal bridge connecting ChatGPT and AI assistants to local, self-hosted, and cloud OpenHands agent environments through MCP. Extensible, secure, and customizable.**

OpenHands Connect bridges conversational AI clients to OpenHands execution environments. It aims to let users explicitly select an environment, dispatch work, observe progress, and retrieve results without coupling the client to runtime-specific APIs.

> **Status:** Project foundation / design phase. Planned capabilities are not claims of implementation.

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
1. Named environment configuration.
2. Provider contract and OpenHands adapter.
3. Environment listing and health tools.
4. Task create/status/logs/message/stop.
5. Policy checks and audit events.
6. Mock-based tests and setup documentation.

## Security
No credentials in prompts, tool outputs, logs, or committed files. Environment selection is explicit; local and cloud permissions remain separate. Remote connectivity must use authenticated secure transport or relay. Higher-impact operations require explicit policy and approval.

See [Project Charter](docs/PROJECT_CHARTER.md), [Architecture](docs/ARCHITECTURE.md), [Roadmap](docs/ROADMAP.md), [Security](docs/SECURITY.md), [Contributing](CONTRIBUTING.md), and [ADR 0001](docs/adr/0001-mcp-as-client-interface.md).

## Development
The language, packaging, and runtime choices will be documented in an ADR before implementation. Start with a small, testable modular design.

## License
MIT. See [LICENSE](LICENSE).
