"""Policy evaluation.

Policy is decided from configuration alone — never from the caller's phrasing.
Defaults deny: an environment must opt in to each execution operation, and a
disabled environment permits nothing.
"""

from __future__ import annotations

from openhands_connect.domain.models import Environment, Operation, PolicyDecision


class PolicyEngine:
    def evaluate(self, environment: Environment, operation: Operation) -> PolicyDecision:
        if not environment.enabled:
            return PolicyDecision(
                allowed=False,
                operation=operation,
                environment_id=environment.id,
                reason=f"environment {environment.id!r} is disabled",
            )
        if operation not in environment.allowed_operations:
            return PolicyDecision(
                allowed=False,
                operation=operation,
                environment_id=environment.id,
                reason=(
                    f"operation {operation.value!r} is not permitted for environment "
                    f"{environment.id!r}; allowed: "
                    f"{sorted(op.value for op in environment.allowed_operations) or 'none'}"
                ),
            )
        return PolicyDecision(
            allowed=True,
            operation=operation,
            environment_id=environment.id,
            reason=f"operation {operation.value!r} permitted for environment {environment.id!r}",
        )
