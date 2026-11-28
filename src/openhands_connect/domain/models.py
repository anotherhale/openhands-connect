"""Provider-neutral domain models for OpenHands Connect.

Nothing in this module may import an MCP type or an OpenHands API type: the
core describes what the bridge *means*, adapters describe how to talk to a
particular runtime.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENVIRONMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
"""Environment IDs are user-facing, so they must be safe to echo in tool output."""

TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

EnvironmentId = Annotated[str, Field(pattern=ENVIRONMENT_ID_PATTERN.pattern)]
TaskId = Annotated[str, Field(pattern=TASK_ID_PATTERN.pattern, max_length=128)]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class ProviderKind(StrEnum):
    OPENHANDS = "openhands"


class TaskState(StrEnum):
    """Normalized lifecycle state.

    ``UNKNOWN`` exists so that an unrecognized or unreadable upstream status is
    never silently reported as a healthy state.
    """

    REQUESTED = "requested"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})


class Operation(StrEnum):
    """Operations a policy can permit or deny per environment."""

    LIST_ENVIRONMENTS = "list_environments"
    CHECK_ENVIRONMENT = "check_environment"
    CREATE_TASK = "create_task"
    GET_TASK = "get_task"
    GET_TASK_LOGS = "get_task_logs"
    SEND_TASK_MESSAGE = "send_task_message"
    STOP_TASK = "stop_task"
    GET_TASK_RESULT = "get_task_result"


READ_OPERATIONS: frozenset[Operation] = frozenset(
    {
        Operation.LIST_ENVIRONMENTS,
        Operation.CHECK_ENVIRONMENT,
        Operation.GET_TASK,
        Operation.GET_TASK_LOGS,
        Operation.GET_TASK_RESULT,
    }
)

#: Operations that can change an execution environment. Denied unless a
#: configuration file explicitly opts the environment in.
EXECUTION_OPERATIONS: frozenset[Operation] = frozenset(
    {
        Operation.CREATE_TASK,
        Operation.SEND_TASK_MESSAGE,
        Operation.STOP_TASK,
    }
)


class Environment(Frozen):
    """A named, explicitly configured execution target."""

    id: EnvironmentId
    name: str = Field(min_length=1, max_length=80)
    provider: ProviderKind
    base_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    allowed_operations: frozenset[Operation] = Field(default_factory=frozenset)
    credential_env_var: str | None = Field(default=None, max_length=128)
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    #: Upper bound on attempts for a *retryable* call (1 disables retries).
    #: Retries are only ever spent on reads and on writes proven idempotent, so
    #: this cannot duplicate a task or a message.
    max_attempts: int = Field(default=3, ge=1, le=10)
    default_agent_profile_id: str | None = Field(default=None, max_length=128)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    def permits(self, operation: Operation) -> bool:
        return self.enabled and operation in self.allowed_operations


class CredentialState(StrEnum):
    """Whether the configured credential was accepted.

    Kept separate from ``reachable`` so a caller can tell "the server is down"
    from "the server is up but my token is wrong" without parsing prose.
    """

    #: No credential is configured for this environment.
    NOT_CONFIGURED = "not_configured"
    #: A credential is configured and the server accepted it.
    ACCEPTED = "accepted"
    #: A credential is configured and the server rejected it (401/403).
    REJECTED = "rejected"
    #: A credential is configured but could not be checked (server error,
    #: timeout, or an unexpected status). Never reported as accepted.
    UNKNOWN = "unknown"


class HealthStatus(Frozen):
    reachable: bool
    detail: str
    server_version: str | None = None
    credential_state: CredentialState = CredentialState.NOT_CONFIGURED
    #: ``/ready`` answers 200 only after initialization and 503 while starting.
    #: Kept separate from ``reachable`` so a server that is up but still
    #: initializing is not reported as ready to accept work. ``None`` when the
    #: readiness probe could not be run (older server without ``/ready``).
    ready: bool | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskRequest(Frozen):
    prompt: str = Field(min_length=1, max_length=20_000)
    workspace: str | None = Field(default=None, max_length=1024)
    max_iterations: int | None = Field(default=None, ge=1, le=500)
    #: Name or ID of an agent profile configured on the target server.
    agent_profile_id: str | None = Field(default=None, max_length=128)
    #: Explicit agent settings, used only when no profile is available.
    #: Passed through to upstream unchanged.
    #: Not exposed by the ``create_task`` MCP tool: the payload is arbitrary and
    #: unvalidated, so accepting it from a caller would let an MCP client set
    #: upstream agent configuration the policy engine cannot inspect. Set here by
    #: a future server-side default, or wire it to the tool with validation first.
    agent_settings: dict[str, Any] | None = None


class Task(Frozen):
    """A task as the bridge understands it, not as upstream serializes it."""

    id: TaskId
    environment_id: EnvironmentId
    state: TaskState
    created_at: datetime | None = None
    updated_at: datetime | None = None
    title: str | None = None
    #: Upstream's own status string, preserved verbatim so callers can tell
    #: "bridge says running" apart from "upstream says running".
    provider_state: str | None = None
    detail: str | None = None


class TaskLogEntry(Frozen):
    timestamp: str | None = None
    kind: str
    source: str | None = None
    text: str


class LogSortOrder(StrEnum):
    """Which end of a task's history a page is read from.

    ``NEWEST_FIRST`` is the default because a long run's recent activity is
    what a caller almost always wants, and reading from the oldest end hides it.
    """

    NEWEST_FIRST = "newest_first"
    OLDEST_FIRST = "oldest_first"


class TaskLogs(Frozen):
    task_id: TaskId
    environment_id: EnvironmentId
    entries: tuple[TaskLogEntry, ...]
    #: True only when entries were fetched but could not be returned *and* no
    #: cursor can reach them. The bridge never advances past an entry it did not
    #: return, so this stays False unless upstream breaks its own ``limit``
    #: contract. Use ``has_more`` to learn that older history exists.
    truncated: bool = False
    dropped_entries: int = 0
    #: Opaque cursor for the next (older, for ``newest_first``) page, or ``None``
    #: when the end of the history has been reached. Callers must treat it as a
    #: black box: its shape is a bridge implementation detail and may change.
    next_cursor: str | None = None
    #: True when at least one older page exists. Derived from ``next_cursor`` so
    #: a caller can loop without parsing the cursor.
    has_more: bool = False
    sort_order: LogSortOrder = LogSortOrder.NEWEST_FIRST


class TaskResult(Frozen):
    task_id: TaskId
    environment_id: EnvironmentId
    state: TaskState
    available: bool
    summary: str | None = None
    detail: str | None = None


class StopResult(Frozen):
    task_id: TaskId
    environment_id: EnvironmentId
    state: TaskState
    #: False when upstream cannot truly terminate a task, so a caller is never
    #: told "cancelled" when the runtime only paused.
    cancelled: bool
    detail: str


class MessageDelivery(Frozen):
    """What actually happened to a follow-up message.

    A message write and the run that consumes it are two upstream operations.
    The message can be stored while the run that would act on it is refused
    (429 when the server's run capacity is full), so the two outcomes are
    reported separately instead of collapsing into one "sent" flag.
    """

    task_id: TaskId
    environment_id: EnvironmentId
    #: Upstream stored the message. A rejected save never produces a result.
    saved: bool
    #: Upstream accepted the request to run. ``None`` when the run could not be
    #: started *and* could not be shown not to have started.
    started: bool | None
    detail: str


class ProviderErrorKind(StrEnum):
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    UNSUPPORTED = "unsupported"
    #: A mutating call whose outcome is genuinely undetermined: the request may
    #: or may not have been applied. Never reported as success, and a retry by
    #: the caller could duplicate the effect.
    AMBIGUOUS = "ambiguous"


class ProviderError(Exception):
    """Base error crossing the adapter boundary.

    ``message`` must already be safe to show: adapters redact credentials
    before raising.
    """

    kind: ProviderErrorKind = ProviderErrorKind.UNAVAILABLE

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable

    def __str__(self) -> str:
        return self.message


class ProviderUnauthorized(ProviderError):
    kind = ProviderErrorKind.UNAUTHORIZED


class ProviderNotFound(ProviderError):
    kind = ProviderErrorKind.NOT_FOUND


class ProviderInvalidRequest(ProviderError):
    kind = ProviderErrorKind.INVALID_REQUEST


class ProviderRateLimited(ProviderError):
    kind = ProviderErrorKind.RATE_LIMITED


class ProviderUnavailable(ProviderError):
    """Upstream is reachable but failing (5xx). Retrying may succeed."""

    kind = ProviderErrorKind.UNAVAILABLE

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message, retryable=retryable)


class ProviderTimeout(ProviderError):
    """The request exceeded the configured timeout. Retrying may succeed."""

    kind = ProviderErrorKind.TIMEOUT

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message, retryable=retryable)


class ProviderProtocolError(ProviderError):
    kind = ProviderErrorKind.PROTOCOL


class ProviderUnsupported(ProviderError):
    """Raised when upstream cannot do what was asked.

    Reporting this beats fabricating a success the runtime never performed.
    """

    kind = ProviderErrorKind.UNSUPPORTED


class ProviderAmbiguousWrite(ProviderError):
    """A mutating request whose effect upstream may or may not have applied.

    Raised instead of a plain timeout/unavailable error when a write fails in a
    way that leaves the outcome undetermined (a timeout, a reset connection, or
    a 5xx *after* the request was sent). Neither success nor failure can be
    claimed, so ``retryable`` is False: blindly retrying could duplicate the
    write. When the caller can supply a stable id the effect is keyed on
    (``task_id``), it is carried here so a caller can check before retrying.
    """

    kind = ProviderErrorKind.AMBIGUOUS

    def __init__(self, message: str, *, task_id: str | None = None) -> None:
        super().__init__(message, retryable=False)
        self.task_id = task_id


class PolicyDecision(Frozen):
    allowed: bool
    operation: Operation
    environment_id: EnvironmentId
    reason: str


class AuditOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The operation's effect could not be determined (an ambiguous write). Not
    #: ``failed``: recording that would assert the write did not land when it may
    #: have, and an operator reading the trail would draw the wrong conclusion.
    UNKNOWN = "unknown"


class AuditEvent(Frozen):
    """Metadata-only audit record.

    Deliberately has no field for prompt text, task output, or credentials:
    the type makes leaking them impossible rather than merely discouraged.
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    environment_id: str
    operation: Operation
    outcome: AuditOutcome
    task_id: str | None = None
    detail: str | None = None
    principal: str = "local"
    schema_version: Literal[1] = 1
