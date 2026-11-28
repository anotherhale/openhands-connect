# Security

## Threat model (initial)

OpenHands Connect bridges an AI client to an execution environment that may read or modify code and run commands. Treat this bridge as a high-trust boundary.

Threats include prompt-injected instructions, unauthorized environment selection, credential exposure, unsafe remote endpoints, task misuse, excessive logs, and accidental high-impact actions.

## Baseline controls

- Bind locally by default during initial development.
- Remote access must be authenticated and encrypted.
- Do not expose raw OpenHands APIs directly to an AI client.
- Use explicit environment identifiers and per-environment policy.
- Keep credentials out of source control, tool output, prompts, and logs.
- Validate and bound task inputs.
- Apply timeouts, cancellation, and resource limits where supported.
- Redact sensitive log content.
- Record task and control actions for audit.
- Deny commit/merge/deploy and other high-impact actions by default unless later explicitly implemented with separate approval controls.

## Approval tiers (proposed)

| Tier | Examples | Default |
|---|---|---|
| Read / observe | List configured environments, health, task status | Allowed for configured targets |
| Task execution | Create task, send follow-up message | Allowed only by environment policy |
| High impact | Commit, PR creation, merge, deployment, destructive operations | Disabled / approval required |

## Credential handling

Configuration should contain credential references, not secrets. The first implementation should use environment variables or a local secure credential source appropriate to the runtime. Never echo credential values in diagnostics.

Each environment resolves its own credential from its own variable. A missing token fails only that environment; it is never filled from another environment, and there is no fallback between environments.

A credential embedded in a URL (`https://user:pass@host`) is refused at load time, and the configuration error shows only the scheme and host so the password is never echoed back. Redaction at the MCP boundary also strips URL userinfo as defence in depth, since `list_environments` returns `base_url`.

## Transport

A remote environment must use `https`. The credential travels in the `X-Session-API-Key` header on every request, so a plaintext remote endpoint would expose the token to anything on the path. `http` is permitted only for loopback hosts (`localhost`, `127.0.0.0/8`, `::1`), where the traffic does not leave the machine.

Loopback detection parses the address rather than matching text, so a lookalike hostname such as `127.0.0.1.nip.io` or `localhost.attacker.example` does not inherit the exemption.

This is enforced by the bridge on its own configuration. It is not a sandbox: the bridge cannot make the upstream server safer than it is, and cannot prevent an operator from reaching that server by other means.

## Logging

Audit logs should prefer metadata (environment ID, task ID, action, result, timestamp) over raw task prompts or outputs. Any payload logging must be explicitly configured and redacted.

## Reporting

Please report vulnerabilities privately to the maintainers. Do not publish sensitive details in public issues.
