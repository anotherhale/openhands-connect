# Security Policy

## Supported Versions

Security fixes are applied to the latest development version unless otherwise stated.

## Reporting a Vulnerability

Please **do not** report security vulnerabilities through public GitHub issues.

Use GitHub's **Private vulnerability reporting** feature for this repository when available. If private reporting is unavailable, contact the repository owner privately through GitHub.

Please include:
- A description of the issue and its potential impact.
- Steps to reproduce, including a minimal proof of concept when safe.
- Affected versions, commits, or configuration.
- Any suggested mitigation.

Please allow maintainers reasonable time to investigate and coordinate a fix before public disclosure.

## Security Design Principles

OpenHands Connect is intended to bridge conversational AI clients and agent execution environments. Security-sensitive design principles include:

- Explicit environment selection; no implicit routing between local and cloud environments.
- Least-privilege credentials and permissions.
- No secrets in prompts, tool responses, logs, or repository files.
- No unauthenticated public exposure of OpenHands endpoints.
- Policy checks and approval gates for consequential operations.
- Auditable task lifecycle and control events.
- Treat external content, repository content, and agent output as untrusted input.
