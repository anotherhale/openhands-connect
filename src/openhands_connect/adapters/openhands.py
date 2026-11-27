"""OpenHands Agent Server adapter.

Every endpoint used here was verified against a running OpenHands Agent Server
1.60.1 and its published OpenAPI document; see
``docs/adr/0002-openhands-agent-server-api.md``. Nothing here guesses a route.

Compatibility note: the bridge targets the *Agent Server* (``agent-server``
package, routes under ``/api``), not the Sandbox Server control plane
(``/api/v1``) and not the archived V0 Local GUI API.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from openhands_connect.domain.errors import InvalidInputError
from openhands_connect.domain.models import (
    Environment,
    HealthStatus,
    ProviderError,
    ProviderInvalidRequest,
    ProviderNotFound,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderUnauthorized,
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
from openhands_connect.redaction import Redactor

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-Session-API-Key"
MAX_LOG_TEXT_CHARS = 2_000
MAX_LOG_ENTRIES = 200

#: Upstream ``execution_status`` -> bridge state. Verified against
#: ``ConversationExecutionStatus`` in the server OpenAPI document.
STATE_MAP: dict[str, TaskState] = {
    "idle": TaskState.DISPATCHED,
    "running": TaskState.RUNNING,
    "paused": TaskState.PAUSED,
    "waiting_for_confirmation": TaskState.WAITING_FOR_CONFIRMATION,
    "finished": TaskState.COMPLETED,
    "error": TaskState.FAILED,
    "stuck": TaskState.FAILED,
    # Deletion is not cancellation. Upstream gives no verified signal that a
    # `deleting` conversation was cancelled, so it stays UNKNOWN; the raw
    # provider_state is preserved on the Task for callers that need it.
    "deleting": TaskState.UNKNOWN,
}

CredentialProvider = Callable[[Environment], "str | None"]


def map_state(provider_state: str | None) -> TaskState:
    """Map an upstream status, preserving ``unknown`` for anything unrecognized.

    A new upstream status must surface as ``unknown`` rather than being
    optimistically read as success or failure.
    """
    if provider_state is None:
        return TaskState.UNKNOWN
    return STATE_MAP.get(provider_state, TaskState.UNKNOWN)


class OpenHandsAdapter:
    """Talks to the OpenHands Agent Server named by each ``Environment``.

    Credentials come from an injected provider rather than being held on the
    adapter, so a token is fetched per call and never stored on an object that
    could be repr'd into a log. The target always comes from the ``Environment``
    argument, so a task can never land on a server other than the one named.
    """

    def __init__(
        self,
        credential_provider: CredentialProvider | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._credential_provider = credential_provider or (lambda _env: None)
        self._transport = transport
        self._redactor = redactor or Redactor()

    def _redactor_for(self, environment: Environment) -> Redactor:
        """A redactor that knows this environment's credential.

        The credential is composed onto the injected redactor's literal set
        rather than replacing it, so configured secrets stay scrubbed. This is
        defence in depth: callers should also configure a redactor, but the
        adapter never relies on them having done so.
        """
        credential = self._credential_provider(environment)
        if not credential:
            return self._redactor
        return self._redactor.with_secrets(credential)

    async def health_check(self, environment: Environment) -> HealthStatus:
        try:
            async with self._client(environment) as client:
                response = await client.get("/server_info")
                if response.status_code in (401, 403):
                    return HealthStatus(
                        reachable=True,
                        detail="server reachable but rejected the credential",
                    )
                self._raise_for_status(response, environment)
                payload = _json_object(response, environment)
        except ProviderError as exc:
            return HealthStatus(reachable=False, detail=exc.message)
        except httpx.HTTPError as exc:
            return HealthStatus(reachable=False, detail=_transport_detail(exc))
        version = payload.get("version")
        return HealthStatus(
            reachable=True,
            detail="agent server reachable",
            server_version=str(version) if version is not None else None,
        )

    async def create_task(self, environment: Environment, request: TaskRequest) -> Task:
        payload: dict[str, Any] = {
            "workspace": {
                "kind": "LocalWorkspace",
                "working_dir": request.workspace or ".",
            },
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": request.prompt}],
                # Without run=True upstream stores the message but never starts
                # the agent loop, so the task would sit idle forever.
                "run": True,
            },
        }
        if request.max_iterations is not None:
            payload["max_iterations"] = request.max_iterations

        # Upstream requires exactly one of agent / agent_settings /
        # agent_profile_id (422 otherwise). Prefer a named profile, then the
        # environment's configured default, then explicit agent_settings.
        profile = request.agent_profile_id or environment.default_agent_profile_id
        if profile:
            payload["agent_profile_id"] = profile
        elif request.agent_settings is not None:
            payload["agent_settings"] = request.agent_settings
        else:
            raise InvalidInputError(
                "no agent configured: provide agent_profile_id, set "
                "default_agent_profile_id on the environment, or pass agent_settings. "
                "The OpenHands Agent Server rejects a conversation with none of them."
            )

        async with self._client(environment) as client:
            if profile and not _looks_like_uuid(profile):
                payload["agent_profile_id"] = await self._resolve_profile_id(
                    client, environment, profile
                )
            response = await client.post("/api/conversations", json=payload)
            self._raise_for_status(response, environment)
            body = _json_object(response, environment)
        return _task_from_payload(body, environment)

    async def _resolve_profile_id(
        self, client: httpx.AsyncClient, environment: Environment, name: str
    ) -> str:
        """Turn a configured profile name into the UUID upstream demands.

        Upstream rejects a non-UUID ``agent_profile_id`` with a 422 that names
        only a parse failure, so a config file holding a readable name would
        otherwise fail with an error that never mentions profiles.
        """
        response = await client.get("/api/agent-profiles")
        self._raise_for_status(response, environment)
        body = _json_object(response, environment)
        raw = body.get("profiles", body.get("items", body.get("agent_profiles")))
        candidates = raw if isinstance(raw, list) else [raw] if raw else []
        known: list[str] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            profile_name = item.get("name")
            profile_id = item.get("id")
            if isinstance(profile_name, str):
                known.append(profile_name)
            if profile_name == name and isinstance(profile_id, str):
                return profile_id
        available = ", ".join(sorted(known)) or "none reported by the server"
        raise InvalidInputError(
            f"agent profile {name!r} does not exist on this environment "
            f"(available: {available}). Set agent_profile_id to a profile name "
            f"or UUID, or set default_agent_profile_id to a valid profile."
        )

    async def get_task(self, environment: Environment, task_id: TaskId) -> Task:
        async with self._client(environment) as client:
            response = await client.get(f"/api/conversations/{task_id}")
            self._raise_for_status(response, environment)
            body = _json_object(response, environment)
        return _task_from_payload(body, environment)

    async def get_task_logs(
        self, environment: Environment, task_id: TaskId, *, limit: int = 50
    ) -> TaskLogs:
        bounded = max(1, min(limit, MAX_LOG_ENTRIES))
        async with self._client(environment) as client:
            response = await client.get(
                f"/api/conversations/{task_id}/events/search",
                params={"limit": bounded, "sort_order": "TIMESTAMP"},
            )
            self._raise_for_status(response, environment)
            body = _json_object(response, environment)

        raw_items = body.get("items")
        if not isinstance(raw_items, list):
            raise ProviderProtocolError(f"upstream returned no event list for task {task_id!r}")

        entries = tuple(
            entry
            for entry in (_log_entry(item, self._redactor_for(environment)) for item in raw_items)
            if entry is not None
        )
        return TaskLogs(
            task_id=task_id,
            environment_id=environment.id,
            entries=entries[:bounded],
            truncated=len(entries) > bounded,
            dropped_entries=max(0, len(entries) - bounded),
        )

    async def get_task_result(self, environment: Environment, task_id: TaskId) -> TaskResult:
        async with self._client(environment) as client:
            task_response = await client.get(f"/api/conversations/{task_id}")
            self._raise_for_status(task_response, environment)
            task = _task_from_payload(_json_object(task_response, environment), environment)

            if task.state is not TaskState.COMPLETED:
                return TaskResult(
                    task_id=task_id,
                    environment_id=environment.id,
                    state=task.state,
                    available=False,
                    detail=f"no final response while task state is {task.state.value}",
                )

            response = await client.get(f"/api/conversations/{task_id}/agent_final_response")
            self._raise_for_status(response, environment)
            body = _json_object(response, environment)

        response_text = body.get("response")
        if not isinstance(response_text, str) or not response_text.strip():
            return TaskResult(
                task_id=task_id,
                environment_id=environment.id,
                state=task.state,
                available=False,
                detail="upstream reported completion but returned an empty final response",
            )
        return TaskResult(
            task_id=task_id,
            environment_id=environment.id,
            state=task.state,
            available=True,
            summary=self._redactor_for(environment).redact(response_text),
        )

    async def send_message(self, environment: Environment, task_id: TaskId, message: str) -> None:
        async with self._client(environment) as client:
            response = await client.post(
                f"/api/conversations/{task_id}/events",
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": message}],
                    "run": True,
                },
            )
            self._raise_for_status(response, environment)

    async def stop_task(self, environment: Environment, task_id: TaskId) -> StopResult:
        """Pause the task, and say plainly that it was not terminated.

        The Agent Server exposes ``interrupt`` and ``pause``, both of which land
        the conversation in ``paused``; no endpoint terminates a run. Reporting
        ``cancelled=True`` would claim something upstream cannot do, so the
        result keeps ``cancelled=False`` and states the real effect.
        """
        async with self._client(environment) as client:
            response = await client.post(f"/api/conversations/{task_id}/interrupt")
            self._raise_for_status(response, environment)
            status_response = await client.get(f"/api/conversations/{task_id}")
            self._raise_for_status(status_response, environment)
            task = _task_from_payload(_json_object(status_response, environment), environment)

        return StopResult(
            task_id=task_id,
            environment_id=environment.id,
            state=task.state,
            cancelled=False,
            detail=(
                "OpenHands Agent Server does not support terminating a task; "
                f"the run was interrupted and is now {task.state.value}."
            ),
        )

    async def aclose(self) -> None:
        """No persistent client is held, so there is nothing to release."""

    def _client(self, environment: Environment) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        credential = self._credential_provider(environment)
        if credential:
            headers[API_KEY_HEADER] = credential
        return httpx.AsyncClient(
            base_url=environment.base_url,
            headers=headers,
            timeout=httpx.Timeout(environment.request_timeout_seconds),
            transport=self._transport,
            follow_redirects=False,
        )

    def _raise_for_status(self, response: httpx.Response, environment: Environment) -> None:
        if response.is_success:
            return
        status = response.status_code
        detail = self._redactor_for(environment).redact(_safe_error_detail(response))
        target = environment.id
        if status in (401, 403):
            raise ProviderUnauthorized(
                f"environment {target!r} rejected the credential ({status}): {detail}"
            )
        if status == 404:
            raise ProviderNotFound(f"not found on environment {target!r} (404): {detail}")
        if status == 429:
            raise ProviderRateLimited(
                f"environment {target!r} rate limited the request (429): {detail}",
                retryable=True,
            )
        if 400 <= status < 500:
            raise ProviderInvalidRequest(
                f"environment {target!r} rejected the request ({status}): {detail}"
            )
        if status >= 500:
            raise ProviderUnavailable(
                f"environment {target!r} failed with {status}: {detail}", retryable=True
            )
        raise ProviderProtocolError(
            f"environment {target!r} returned unexpected status {status}: {detail}"
        )


def _task_from_payload(payload: dict[str, Any], environment: Environment) -> Task:
    task_id = payload.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ProviderProtocolError(
            f"upstream response for environment {environment.id!r} had no conversation id"
        )
    provider_state = payload.get("execution_status")
    provider_state_str = provider_state if isinstance(provider_state, str) else None
    title = payload.get("title")
    return Task(
        id=task_id,
        environment_id=environment.id,
        state=map_state(provider_state_str),
        created_at=_timestamp(payload.get("created_at")),
        updated_at=_timestamp(payload.get("updated_at")),
        title=title if isinstance(title, str) else None,
        provider_state=provider_state_str,
        detail=(
            None if provider_state_str in STATE_MAP else _unknown_state_detail(provider_state_str)
        ),
    )


def _unknown_state_detail(provider_state: str | None) -> str:
    if provider_state is None:
        return "upstream returned no execution status; state is unknown"
    return f"upstream reported unrecognized execution status {provider_state!r}"


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _log_entry(item: object, redactor: Redactor) -> TaskLogEntry | None:
    if not isinstance(item, dict):
        return None
    kind = item.get("kind")
    if not isinstance(kind, str):
        return None
    text = _summarize_event(item)
    if text is None:
        return None
    if len(text) > MAX_LOG_TEXT_CHARS:
        text = text[:MAX_LOG_TEXT_CHARS] + "…[truncated]"
    timestamp = item.get("timestamp")
    source = item.get("source")
    return TaskLogEntry(
        timestamp=timestamp if isinstance(timestamp, str) else None,
        kind=kind,
        source=source if isinstance(source, str) else None,
        text=redactor.redact(text),
    )


def _summarize_event(item: dict[str, Any]) -> str | None:
    """Render one upstream event as bounded text.

    Handles the event shapes the bridge surfaces (messages, actions,
    observations, errors, state updates) and falls back to the event kind so an
    unrecognized event still appears rather than vanishing from the log.
    """
    kind = item.get("kind")

    if kind == "MessageEvent":
        message = item.get("llm_message")
        if isinstance(message, dict):
            role = message.get("role", "message")
            text = _content_text(message.get("content"))
            if text:
                return f"{role}: {text}"
        return None

    if kind == "ActionEvent":
        action = item.get("action")
        tool_name = action.get("tool_name") if isinstance(action, dict) else None
        thought = _content_text(item.get("thought"))
        label = f"action {tool_name}" if isinstance(tool_name, str) else "action"
        return f"{label}: {thought}" if thought else label

    if kind == "ObservationEvent":
        tool_name = item.get("tool_name")
        label = f"observation from {tool_name}" if isinstance(tool_name, str) else "observation"
        observation = item.get("observation")
        text = ""
        if isinstance(observation, dict):
            text = str(observation.get("content") or observation.get("text") or "")
        return f"{label}: {text}" if text else label

    if kind == "AgentErrorEvent":
        error = item.get("error")
        return f"agent error: {error}" if isinstance(error, str) else "agent error"

    if kind == "ConversationErrorEvent":
        return f"conversation error ({item.get('code')}): {item.get('detail')}"

    if kind == "ConversationStateUpdateEvent":
        return f"state {item.get('key')}={item.get('value')}"

    if kind == "PauseEvent":
        return "paused by user request"

    return str(kind)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type", "text") == "text"
    ]
    return " ".join(part for part in parts if part)


_UUID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE
)


def _looks_like_uuid(value: str) -> bool:
    """Upstream wants a UUID; anything else must be resolved from a name."""
    return _UUID_RE.match(value) is not None


def _json_object(response: httpx.Response, environment: Environment) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderProtocolError(
            f"environment {environment.id!r} returned a non-JSON response"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderProtocolError(
            f"environment {environment.id!r} returned JSON that is not an object"
        )
    return payload


def _safe_error_detail(response: httpx.Response) -> str:
    """Extract a short, structured error detail without echoing the request."""
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] if text else f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail[:300]
        if isinstance(detail, list):
            messages = [str(item.get("msg", item)) for item in detail[:3] if isinstance(item, dict)]
            if messages:
                return "; ".join(messages)[:300]
    return f"HTTP {response.status_code}"


def _transport_detail(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    return f"connection failed: {type(exc).__name__}"


__all__ = ["API_KEY_HEADER", "STATE_MAP", "OpenHandsAdapter", "map_state"]
