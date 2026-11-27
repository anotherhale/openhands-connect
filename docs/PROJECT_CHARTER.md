# Project Charter

## Mission

Provide a secure, extensible bridge through which ChatGPT and other MCP-capable AI clients can connect to named OpenHands execution environments and delegate, observe, and control tasks.

## Problem

Conversational AI clients and coding-agent execution environments are often disconnected. Users need a consistent, secure way to select an execution target, dispatch a task, track it, and interact with the agent without coupling the client to runtime-specific APIs.

## Product vision

OpenHands Connect is a reusable connectivity product—not a personal one-off integration. Its core should be generic, while users can configure their own local, self-hosted, or cloud environments and add custom workflows.

## Users

- Developers using ChatGPT or other MCP-compatible clients as an interaction surface.
- Individuals and teams running local or remote OpenHands environments.
- Integrators who need a reusable connector for agent execution.

## Core user journeys

1. Configure one or more named execution environments.
2. Ask the client to list environments and check health.
3. Explicitly choose an environment for a task.
4. Submit a task with a clear request and bounded context.
5. Observe task status and logs.
6. Send additional instructions or cancel.
7. Review completion summary and available artifacts.

## MVP success criteria

- A local OpenHands environment can be configured and validated.
- A cloud or remote environment can be configured separately.
- MCP clients can discover and invoke core tools.
- Task lifecycle is covered by automated tests, including errors and cancellation.
- Environment selection is explicit and auditable.
- Secrets are not exposed in tool outputs or logs.
- Setup instructions allow a new user to configure the bridge without maintainer intervention.

## Product constraints

- OpenHands is the first supported execution provider.
- The core should permit additional providers later without requiring them for MVP.
- No production deployment, commit, merge, or other high-impact action is enabled by default.
- Remote connectivity must use an authenticated, secure transport or relay; no public unauthenticated agent endpoint.
