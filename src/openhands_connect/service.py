"""Application use cases.

Each method resolves the environment, evaluates policy *before* touching the
provider, emits audit metadata, and returns a domain object. This is the only
place that sequences those steps, so no interface can skip policy or audit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from openhands_connect.audit import AuditSink, NullAuditSink
from openhands_connect.domain.errors import ConfigurationError, PolicyDeniedError
from openhands_connect.domain.models import (
    AuditEvent,
    AuditOutcome,
    Environment,
    EnvironmentId,
    HealthStatus,
    MessageDelivery,
    Operation,
    ProviderError,
    StopResult,
    Task,
    TaskId,
    TaskLogs,
    TaskRequest,
    TaskResult,
)
from openhands_connect.domain.provider import AgentExecutionProvider
from openhands_connect.domain.registry import EnvironmentRegistry
from openhands_connect.policy import PolicyEngine

_T = TypeVar("_T")


@dataclass(frozen=True)
class ConnectService:
    registry: EnvironmentRegistry
    provider: AgentExecutionProvider
    policy: PolicyEngine
    audit: AuditSink = field(default_factory=NullAuditSink)

    # -- read-only operations -------------------------------------------------

    def list_environments(self) -> tuple[Environment, ...]:
        """Return configured environments.

        Listing is not an operation against a target, so it needs no policy
        check; callers still see only sanitized metadata.
        """
        return self.registry.list()

    async def check_environment(self, environment_id: EnvironmentId) -> HealthStatus:
        environment = self._authorize(environment_id, Operation.CHECK_ENVIRONMENT)
        return await self._run(
            environment,
            Operation.CHECK_ENVIRONMENT,
            lambda: self.provider.health_check(environment),
        )

    async def get_task(self, environment_id: EnvironmentId, task_id: TaskId) -> Task:
        environment = self._authorize(environment_id, Operation.GET_TASK)
        return await self._run(
            environment,
            Operation.GET_TASK,
            lambda: self.provider.get_task(environment, task_id),
            task_id=task_id,
        )

    async def get_task_logs(
        self, environment_id: EnvironmentId, task_id: TaskId, *, limit: int = 50
    ) -> TaskLogs:
        environment = self._authorize(environment_id, Operation.GET_TASK_LOGS)
        return await self._run(
            environment,
            Operation.GET_TASK_LOGS,
            lambda: self.provider.get_task_logs(environment, task_id, limit=limit),
            task_id=task_id,
        )

    async def get_task_result(self, environment_id: EnvironmentId, task_id: TaskId) -> TaskResult:
        environment = self._authorize(environment_id, Operation.GET_TASK_RESULT)
        return await self._run(
            environment,
            Operation.GET_TASK_RESULT,
            lambda: self.provider.get_task_result(environment, task_id),
            task_id=task_id,
        )

    # -- execution operations -------------------------------------------------

    async def create_task(self, environment_id: EnvironmentId, request: TaskRequest) -> Task:
        environment = self._authorize(environment_id, Operation.CREATE_TASK)
        return await self._run(
            environment,
            Operation.CREATE_TASK,
            lambda: self.provider.create_task(environment, request),
        )

    async def send_task_message(
        self, environment_id: EnvironmentId, task_id: TaskId, message: str
    ) -> MessageDelivery:
        environment = self._authorize(environment_id, Operation.SEND_TASK_MESSAGE)
        return await self._run(
            environment,
            Operation.SEND_TASK_MESSAGE,
            lambda: self.provider.send_message(environment, task_id, message),
            task_id=task_id,
        )

    async def stop_task(self, environment_id: EnvironmentId, task_id: TaskId) -> StopResult:
        environment = self._authorize(environment_id, Operation.STOP_TASK)
        return await self._run(
            environment,
            Operation.STOP_TASK,
            lambda: self.provider.stop_task(environment, task_id),
            task_id=task_id,
        )

    # -- internals ------------------------------------------------------------

    def _authorize(self, environment_id: EnvironmentId, operation: Operation) -> Environment:
        environment = self.registry.get(environment_id)
        decision = self.policy.evaluate(environment, operation)
        if not decision.allowed:
            self.audit.record(
                AuditEvent(
                    environment_id=environment.id,
                    operation=operation,
                    outcome=AuditOutcome.DENIED,
                    detail=decision.reason,
                )
            )
            raise PolicyDeniedError(decision.reason)
        return environment

    async def _run(
        self,
        environment: Environment,
        operation: Operation,
        action: Callable[[], Awaitable[_T]],
        *,
        task_id: TaskId | None = None,
    ) -> _T:
        try:
            result = await action()
        except ProviderError as exc:
            self.audit.record(
                AuditEvent(
                    environment_id=environment.id,
                    operation=operation,
                    outcome=AuditOutcome.FAILED,
                    task_id=task_id,
                    # ProviderError messages are redacted at the adapter
                    # boundary, so this is safe to persist.
                    detail=exc.message,
                )
            )
            raise
        except ConfigurationError as exc:
            # A configured-but-unusable environment is an operator fault, not an
            # upstream failure. ConfigurationError messages are built only from
            # config identifiers (env var names, file paths), never values, so
            # the message is safe to record and is what an operator needs.
            self.audit.record(
                AuditEvent(
                    environment_id=environment.id,
                    operation=operation,
                    outcome=AuditOutcome.FAILED,
                    task_id=task_id,
                    detail=exc.message,
                )
            )
            raise
        except Exception as exc:
            # An unexpected failure must still leave an audit trail, or a failed
            # operation silently disappears from it. Only the exception *type*
            # is recorded: a message from an arbitrary exception may carry
            # request data or a credential, and the audit store has no redactor.
            self.audit.record(
                AuditEvent(
                    environment_id=environment.id,
                    operation=operation,
                    outcome=AuditOutcome.FAILED,
                    task_id=task_id,
                    detail=f"unexpected {type(exc).__name__}",
                )
            )
            raise
        self.audit.record(
            AuditEvent(
                environment_id=environment.id,
                operation=operation,
                outcome=AuditOutcome.SUCCEEDED,
                task_id=task_id,
            )
        )
        return result
