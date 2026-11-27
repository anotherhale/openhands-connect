"""A deterministic in-memory provider used by the test suite.

Deliberately not a mock library: it implements the real protocol so tests
exercise the actual service, policy, audit and MCP paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from openhands_connect.domain.models import (
    CredentialState,
    Environment,
    HealthStatus,
    MessageDelivery,
    ProviderNotFound,
    ProviderUnavailable,
    StopResult,
    Task,
    TaskId,
    TaskLogEntry,
    TaskLogs,
    TaskRequest,
    TaskResult,
    TaskState,
)


@dataclass
class FakeProvider:
    """Records calls and returns canned domain objects."""

    reachable: bool = True
    health_detail: str = "fake reachable"
    server_version: str = "9.9.9"
    credential_state: CredentialState = CredentialState.ACCEPTED
    state: TaskState = TaskState.RUNNING
    provider_state: str = "running"
    result_summary: str = "all done"
    result_available: bool = True
    task_title: str = "fake task"
    task_detail: str | None = None
    log_entries: tuple[TaskLogEntry, ...] = ()
    message_started: bool | None = True
    fail_with: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    created_requests: list[TaskRequest] = field(default_factory=list)
    sent_messages: list[tuple[str, str]] = field(default_factory=list)
    closed: bool = False

    def _record(self, operation: str, environment: Environment) -> None:
        self.calls.append((operation, environment.id))
        if self.fail_with is not None:
            raise self.fail_with

    def _task(self, environment: Environment, task_id: TaskId) -> Task:
        return Task(
            id=task_id,
            environment_id=environment.id,
            state=self.state,
            provider_state=self.provider_state,
            title=self.task_title,
            detail=self.task_detail,
        )

    async def health_check(self, environment: Environment) -> HealthStatus:
        self._record("health_check", environment)
        return HealthStatus(
            reachable=self.reachable,
            detail=self.health_detail,
            server_version=self.server_version,
            credential_state=self.credential_state,
        )

    async def create_task(self, environment: Environment, request: TaskRequest) -> Task:
        self._record("create_task", environment)
        self.created_requests.append(request)
        return self._task(environment, "task-0001")

    async def get_task(self, environment: Environment, task_id: TaskId) -> Task:
        self._record("get_task", environment)
        return self._task(environment, task_id)

    async def get_task_logs(
        self, environment: Environment, task_id: TaskId, *, limit: int = 50
    ) -> TaskLogs:
        self._record("get_task_logs", environment)
        entries = self.log_entries[:limit]
        return TaskLogs(
            task_id=task_id,
            environment_id=environment.id,
            entries=entries,
            truncated=len(self.log_entries) > limit,
            dropped_entries=max(0, len(self.log_entries) - limit),
        )

    async def get_task_result(self, environment: Environment, task_id: TaskId) -> TaskResult:
        self._record("get_task_result", environment)
        return TaskResult(
            task_id=task_id,
            environment_id=environment.id,
            state=self.state,
            available=self.result_available,
            summary=self.result_summary if self.result_available else None,
            detail=None if self.result_available else "not available",
        )

    async def send_message(
        self, environment: Environment, task_id: TaskId, message: str
    ) -> MessageDelivery:
        self._record("send_message", environment)
        self.sent_messages.append((task_id, message))
        return MessageDelivery(
            task_id=task_id,
            environment_id=environment.id,
            saved=True,
            started=self.message_started,
            detail=(
                "fake delivery: message saved and the run started"
                if self.message_started
                else "fake delivery: message saved but the run did not start"
            ),
        )

    async def stop_task(self, environment: Environment, task_id: TaskId) -> StopResult:
        self._record("stop_task", environment)
        return StopResult(
            task_id=task_id,
            environment_id=environment.id,
            state=TaskState.PAUSED,
            cancelled=False,
            detail="interrupted and paused",
        )

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FailingProvider(FakeProvider):
    """Provider that fails every call with a chosen error."""

    fail_with: Exception | None = field(default_factory=lambda: ProviderUnavailable("boom"))


@dataclass
class NotFoundProvider(FakeProvider):
    fail_with: Exception | None = field(default_factory=lambda: ProviderNotFound("no such task"))
