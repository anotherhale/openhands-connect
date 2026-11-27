# ADR 0001: MCP as the Initial Client Interface

- **Status:** Accepted for MVP
- **Date:** 2026-11-26

## Context

The project aims to connect conversational AI clients to OpenHands execution environments. MCP provides a standardized tool interface and reduces the need to build a client-specific integration first.

## Decision

Use MCP as the primary interface for the MVP. Keep MCP-facing handlers thin and route requests through core use cases. Do not let MCP-specific types leak into the provider domain.

## Consequences

### Positive

- Compatible clients can consume the tools without a custom client implementation.
- Tool discovery and invocation can be standardized.
- The core can later support additional interfaces.

### Tradeoffs

- Client support and connectivity requirements vary by platform and deployment.
- MCP does not itself provide all authentication, authorization, relay, approval, or audit requirements.
- A secure local-to-cloud connectivity strategy remains a separate design concern.
