# Architecture

## Design principles

- **Modular monolith first:** deploy simply; keep domain boundaries clear.
- **Provider-neutral core:** OpenHands is an adapter, not the domain model.
- **Explicit routing:** tasks target a named environment; no implicit local/cloud fallback.
- **Policy before execution:** authorization and approval checks precede provider calls.
- **Observable by design:** task lifecycle and control actions produce audit events.
- **Minimal data exposure:** tool responses contain only information needed for the caller.

## Components

### MCP interface

Exposes a small, stable tool surface to compatible AI clients. It translates tool requests into core use cases and shapes safe responses.

### Core domain

Owns environment identities, task requests, task state, provider interfaces, policy decisions, and audit events. It should not depend on OpenHands API models.

### Environment registry

Stores configuration for named environments, including provider type, endpoint/connection reference, enabled state, and permitted operations. Credentials are referenced from a secure credential source—not stored in registry data.

### Task manager

Coordinates task creation, status retrieval, log retrieval, follow-up messaging, cancellation, timeout behavior, and provider error translation.

### Policy engine

Checks whether an operation is allowed for the selected environment and whether approval is required. Policy is independent from the AI client's natural-language intent.

### Audit store

Records important events such as environment selection, task submission, message sent, cancellation requested, provider failures, and approval outcomes. Avoid storing credentials and redact sensitive payloads.

### Provider adapter

Implements the execution-provider contract. OpenHands API specifics, authentication details, request/response mapping, and compatibility concerns live here.

## Provider contract (conceptual)

```typescript
interface AgentExecutionProvider {
  connect(): Promise<ConnectionStatus>;
  listEnvironments(): Promise<Environment[]>;
  createTask(request: TaskRequest): Promise<Task>;
  getTask(taskId: string): Promise<Task>;
  getTaskLogs(taskId: string): Promise<TaskLog[]>;
  sendMessage(taskId: string, message: string): Promise<void>;
  stopTask(taskId: string): Promise<void>;
}
```

The first implementation may refine this interface based on verified OpenHands APIs. Do not assume the conceptual interface maps one-to-one to the upstream API.

## Environment model

Each configured target has a stable identifier and human-readable name, such as `little-mac-local` or `openhands-cloud`. Environment selection should be explicit, with connection and authorization state clearly reported.

## Task lifecycle

The bridge should model task state without inventing provider state. Initial conceptual states:

`requested → policy-checked → dispatched → running → (completed | failed | cancelled | timed-out | unknown)`

Provider-specific states should map to these bridge states while preserving a safe provider status field when useful.

## Trust boundaries

- AI client ↔ MCP server
- MCP server ↔ bridge core
- bridge core ↔ provider adapter
- provider adapter ↔ local/remote OpenHands
- policy/approval and audit boundaries

Local and cloud targets should have separate configuration, credentials, permissions, and audit identity.

## Future extension points

- Additional agent execution providers.
- Secure relay/tunnel for local instances behind NAT/firewalls.
- User-defined workflows and customization SDK.
- Optional integration with IEC/Orynth capability management.
- Admin UI, only when the core workflow and configuration model are stable.
