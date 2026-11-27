"""Provider contract.

The core depends on this protocol only; adapters implement it. Kept narrow on
purpose — every method here is one the bridge actually exposes as a tool.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from openhands_connect.domain.models import (
    Environment,
    HealthStatus,
    MessageDelivery,
    StopResult,
    Task,
    TaskId,
    TaskLogs,
    TaskRequest,
    TaskResult,
)


@runtime_checkable
class AgentExecutionProvider(Protocol):
    async def health_check(self, environment: Environment) -> HealthStatus: ...

    async def create_task(self, environment: Environment, request: TaskRequest) -> Task: ...

    async def get_task(self, environment: Environment, task_id: TaskId) -> Task: ...

    async def get_task_logs(
        self, environment: Environment, task_id: TaskId, *, limit: int = 50
    ) -> TaskLogs: ...

    async def get_task_result(self, environment: Environment, task_id: TaskId) -> TaskResult: ...

    async def send_message(
        self, environment: Environment, task_id: TaskId, message: str
    ) -> MessageDelivery: ...

    async def stop_task(self, environment: Environment, task_id: TaskId) -> StopResult: ...

    async def aclose(self) -> None: ...
