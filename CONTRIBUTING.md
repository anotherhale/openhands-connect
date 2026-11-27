# Contributing

## Principles

- Keep the bridge generic; do not hardwire personal deployment details.
- Keep OpenHands API specifics inside the OpenHands adapter.
- Prefer small, testable changes with clear contracts.
- Do not add features that bypass policy, approval, environment selection, or audit requirements.
- Avoid premature microservices, Kubernetes requirements, or large abstractions.

## Workflow

1. Open or reference an issue describing the problem and expected behavior.
2. Propose architecture changes with an ADR when they affect boundaries or contracts.
3. Add tests for behavior and failure paths.
4. Update docs and examples.
5. Run relevant validation before opening a pull request.
6. Include risks, limitations, and evidence in the PR description.

## Security

Never commit credentials, tokens, private endpoint details, or environment-specific configuration. Use sanitized examples. Report security concerns privately to maintainers rather than opening public issues containing exploit details.
