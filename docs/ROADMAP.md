# Roadmap

## v0.1 — Safe functional bridge

Delivered across PRs #4, #10, #11, #13 and #14.

- [x] Repository foundation and project documentation.
- [x] Runtime/language decision and ADR.
- [x] Configuration model for named environments.
- [x] Provider contract.
- [x] OpenHands adapter.
- [x] Environment listing and health tools.
- [x] Task creation, status, logs, messaging, cancellation.
- [x] Policy checks and audit events.
- [x] Mock-based contract and lifecycle tests.
- [x] Local setup and sanitized configuration examples.

## v0.2 — Remote connectivity

- [x] Validate supported remote OpenHands deployment patterns.
  An `https://` Agent Server is reachable directly; no relay is needed when it
  is network-addressable from the client.
- [x] Transport security: TLS required for any non-loopback `base_url`.
- [x] Multiple named environments with per-environment credentials and no failover.
- [x] Connection diagnostics and clearer failure classification.
  A transport failure is reported as one of a fixed set of phrases (TLS
  rejection, DNS failure, closed port, no response) instead of an exception
  class name, so the caller learns the kind of failure without the raw upstream
  text — which can contain a host or URL — reaching tool output.
- [ ] Secure relay/tunnel design and implementation if needed.
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
