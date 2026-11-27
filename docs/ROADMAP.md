# Roadmap

## v0.1 — Safe functional bridge

- [ ] Repository foundation and project documentation.
- [ ] Runtime/language decision and ADR.
- [ ] Configuration model for named environments.
- [ ] Provider contract.
- [ ] OpenHands adapter.
- [ ] Environment listing and health tools.
- [ ] Task creation, status, logs, messaging, cancellation.
- [ ] Policy checks and audit events.
- [ ] Mock-based contract and lifecycle tests.
- [ ] Local setup and sanitized configuration examples.

## v0.2 — Remote connectivity

- [ ] Validate supported remote OpenHands deployment patterns.
- [ ] Secure relay/tunnel design and implementation if needed.
- [ ] Connection diagnostics and clearer failure classification.
- [ ] Credential-provider abstraction.

## v0.3 — Client experience and customization

- [ ] Improve tool names, descriptions, and client ergonomics.
- [ ] Custom workflows and configuration hooks.
- [ ] Document extension contract.
- [ ] Optional ChatGPT-oriented app experience, subject to platform support.

## v0.4 — Multi-provider groundwork

- [ ] Formal provider capability descriptors.
- [ ] Add a second provider only when there is a concrete use case.
- [ ] Capability-aware routing without implicit privilege escalation.

## v1.0 — Stable release

- [ ] Compatibility/versioning policy.
- [ ] Security review and threat model.
- [ ] Reliable lifecycle, audit, and cancellation behavior.
- [ ] Documentation, examples, packaging, release automation.
- [ ] Public contribution and support expectations.

## Deferred / intentionally out of scope

- Multi-agent orchestration platform.
- Autonomous PR merge or production deployment.
- Broad admin console.
- Kubernetes requirement.
- Provider marketplace before extension contracts are proven.
