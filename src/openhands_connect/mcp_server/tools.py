"""MCP tool surface.

Handlers stay thin: validate and shape input, call one service method, return a
plain dict. MCP types do not travel into the core; domain objects are rendered
here. Errors are converted to a stable ``{"error": ..., "message": ...}`` shape
instead of raising, so a caller always receives a readable explanation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from openhands_connect.domain.errors import (
    ConnectError,
    InvalidInputError,
    PolicyDeniedError,
    UnknownEnvironmentError,
)
from openhands_connect.domain.models import (
    Environment,
    LogSortOrder,
    ProviderError,
    Task,
    TaskId,
    TaskLogs,
    TaskRequest,
    TaskResult,
)
from openhands_connect.domain.pagination import MAX_CURSOR_CHARS
from openhands_connect.redaction import Redactor
from openhands_connect.service import ConnectService

MAX_PROMPT_CHARS = 20_000
MAX_MESSAGE_CHARS = 8_000
#: Upstream rejects ``limit`` above 100 with a 422, so a schema offering more
#: would advertise a value that can only fail. A test keeps this in step with
#: the adapter's bound rather than importing it, which would pull an upstream
#: detail into this layer.
MAX_LOG_ENTRIES = 100

EnvironmentIdArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9-]{0,62}$",
        description="ID of a configured environment, exactly as returned by list_environments.",
    ),
]
TaskIdArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        description="Task ID returned by create_task.",
    ),
]
PromptArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_PROMPT_CHARS,
        description="Instruction for the agent. Bounded in length.",
    ),
]
MessageArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_MESSAGE_CHARS,
        description="Follow-up instruction for the running task.",
    ),
]
LimitArg = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_LOG_ENTRIES,
        description=f"Maximum number of log entries to return (1-{MAX_LOG_ENTRIES}).",
    ),
]
CursorArg = Annotated[
    str | None,
    Field(
        default=None,
        max_length=MAX_CURSOR_CHARS,
        description=(
            "Opaque cursor from a previous get_task_logs result's next_cursor. "
            "Pass it unchanged to fetch the next page; omit it to start from the "
            "newest events. A cursor is only valid for the task and sort_order it "
            "was issued for."
        ),
    ),
]
SortOrderArg = Annotated[
    LogSortOrder | None,
    Field(
        default=None,
        description=(
            "Which end of the log to read: 'newest_first' (the default when "
            "starting, showing recent activity) or 'oldest_first' (the opening "
            "events). Omit it when continuing a traversal: the cursor carries its "
            "own direction."
        ),
    ),
]


def _text_redactor(redactor: Redactor | None) -> Callable[[str | None], str | None]:
    """Return a scrubber for outbound free text.

    Applied at this boundary as well as in the adapter, so a value that slips
    past the adapter's own redaction still cannot reach a caller.
    """
    active = redactor or Redactor()
    return lambda text: None if text is None else active.redact(text)


def environment_view(environment: Environment, redactor: Redactor | None = None) -> dict[str, Any]:
    """Sanitized view: never includes a credential value or its env var name.

    ``base_url`` is passed through the redactor: a URL is data, not a literal
    constant, and one carrying ``user:password@`` would otherwise leak a secret
    through a field advertised as safe.
    """
    return {
        "id": environment.id,
        "name": environment.name,
        "provider": environment.provider.value,
        "base_url": (redactor or Redactor()).redact(environment.base_url),
        "enabled": environment.enabled,
        "allowed_operations": sorted(op.value for op in environment.allowed_operations),
    }


def task_view(task: Task, redactor: Redactor | None = None) -> dict[str, Any]:
    redact = _text_redactor(redactor)
    return {
        "task_id": task.id,
        "environment_id": task.environment_id,
        "state": task.state.value,
        "provider_state": task.provider_state,
        "title": redact(task.title),
        "detail": redact(task.detail),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def logs_view(logs: TaskLogs, redactor: Redactor | None = None) -> dict[str, Any]:
    redact = _text_redactor(redactor)
    return {
        "task_id": logs.task_id,
        "environment_id": logs.environment_id,
        "truncated": logs.truncated,
        "dropped_entries": logs.dropped_entries,
        "sort_order": logs.sort_order.value,
        "next_cursor": logs.next_cursor,
        "has_more": logs.has_more,
        "entries": [
            {
                "timestamp": entry.timestamp,
                "kind": entry.kind,
                "source": entry.source,
                "text": redact(entry.text),
            }
            for entry in logs.entries
        ],
    }


def result_view(result: TaskResult, redactor: Redactor | None = None) -> dict[str, Any]:
    redact = _text_redactor(redactor)
    return {
        "task_id": result.task_id,
        "environment_id": result.environment_id,
        "state": result.state.value,
        "available": result.available,
        "summary": redact(result.summary),
        "detail": redact(result.detail),
    }


def error_payload(exc: Exception) -> dict[str, Any]:
    """Map an error to a stable, safe tool payload.

    Never includes a traceback or raw provider body; provider messages are
    already redacted at the adapter boundary.
    """
    if isinstance(exc, UnknownEnvironmentError):
        return {"error": "unknown_environment", "message": str(exc)}
    if isinstance(exc, PolicyDeniedError):
        return {"error": "policy_denied", "message": str(exc)}
    if isinstance(exc, InvalidInputError):
        return {"error": "invalid_input", "message": str(exc)}
    if isinstance(exc, ProviderError):
        return {
            "error": f"provider_{exc.kind.value}",
            "message": str(exc),
            "retryable": exc.retryable,
        }
    if isinstance(exc, ConnectError):
        return {"error": "connect_error", "message": str(exc)}
    return {"error": "internal_error", "message": "unexpected internal error"}


class MCPTools:
    """Thin wrappers around the service; one method per exposed tool."""

    def __init__(self, service: ConnectService, redactor: Redactor | None = None) -> None:
        self._service = service
        self._redactor = redactor or Redactor()

    async def list_environments(self) -> dict[str, Any]:
        environments = self._service.list_environments()
        return {
            "environments": [environment_view(env) for env in environments],
            "count": len(environments),
            "note": (
                "These are the only valid targets. Execution tools require an "
                "explicit environment_id; there is no default and no fallback "
                "between environments."
            ),
        }

    async def check_environment(self, environment_id: EnvironmentIdArg) -> dict[str, Any]:
        try:
            health = await self._service.check_environment(environment_id)
        except Exception as exc:
            return error_payload(exc)
        return {
            "environment_id": environment_id,
            "reachable": health.reachable,
            "ready": health.ready,
            "credential_state": health.credential_state.value,
            "detail": self._redactor.redact(health.detail),
            "server_version": health.server_version,
            "checked_at": health.checked_at.isoformat(),
        }

    async def create_task(
        self,
        environment_id: EnvironmentIdArg,
        prompt: PromptArg,
        workspace: Annotated[
            str | None,
            Field(
                max_length=1024,
                description=(
                    "Absolute working directory for the agent on the target host. "
                    "Defaults to the server's working directory when omitted."
                ),
            ),
        ] = None,
        max_iterations: Annotated[
            int | None,
            Field(ge=1, le=500, description="Optional agent iteration ceiling."),
        ] = None,
        agent_profile_id: Annotated[
            str | None,
            Field(
                max_length=128,
                description=(
                    "Optional agent profile configured on the target server. "
                    "Falls back to the environment's configured default."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        try:
            task = await self._service.create_task(
                environment_id,
                TaskRequest(
                    prompt=prompt,
                    workspace=workspace,
                    max_iterations=max_iterations,
                    agent_profile_id=agent_profile_id,
                ),
            )
        except Exception as exc:
            return error_payload(exc)
        return task_view(task, self._redactor)

    async def get_task(
        self, environment_id: EnvironmentIdArg, task_id: TaskIdArg
    ) -> dict[str, Any]:
        try:
            task = await self._service.get_task(environment_id, task_id)
        except Exception as exc:
            return error_payload(exc)
        return task_view(task, self._redactor)

    async def get_task_logs(
        self,
        environment_id: EnvironmentIdArg,
        task_id: TaskIdArg,
        limit: LimitArg = 50,
        cursor: CursorArg = None,
        sort_order: SortOrderArg = None,
    ) -> dict[str, Any]:
        try:
            logs = await self._service.get_task_logs(
                environment_id,
                task_id,
                limit=limit,
                cursor=cursor,
                sort_order=sort_order,
            )
        except Exception as exc:
            return error_payload(exc)
        return logs_view(logs, self._redactor)

    async def send_task_message(
        self, environment_id: EnvironmentIdArg, task_id: TaskIdArg, message: MessageArg
    ) -> dict[str, Any]:
        try:
            delivery = await self._service.send_task_message(environment_id, task_id, message)
        except Exception as exc:
            return error_payload(exc)
        # ``sent`` means "the run will act on it". A saved message whose run was
        # refused (429 run capacity) is reported as sent=False, started=False,
        # saved=True, so a caller never reads a stored-but-idle message as done.
        return {
            "task_id": delivery.task_id,
            "environment_id": delivery.environment_id,
            "saved": delivery.saved,
            "started": delivery.started,
            "sent": delivery.saved and delivery.started is True,
            "detail": self._redactor.redact(delivery.detail),
        }

    async def stop_task(
        self, environment_id: EnvironmentIdArg, task_id: TaskIdArg
    ) -> dict[str, Any]:
        try:
            result = await self._service.stop_task(environment_id, task_id)
        except Exception as exc:
            return error_payload(exc)
        return {
            "task_id": result.task_id,
            "environment_id": result.environment_id,
            "state": result.state.value,
            "cancelled": result.cancelled,
            "detail": self._redactor.redact(result.detail) if result.detail else None,
        }

    async def get_task_result(
        self, environment_id: EnvironmentIdArg, task_id: TaskIdArg
    ) -> dict[str, Any]:
        try:
            result = await self._service.get_task_result(environment_id, task_id)
        except Exception as exc:
            return error_payload(exc)
        return result_view(result, self._redactor)


__all__ = [
    "MCPTools",
    "TaskId",
    "environment_view",
    "error_payload",
    "logs_view",
    "result_view",
    "task_view",
]
