"""OpenHands Agent Server adapter.

Every endpoint used here was verified against a running OpenHands Agent Server
1.60.1 and its published OpenAPI document; see
``docs/adr/0002-openhands-agent-server-api.md``. Nothing here guesses a route.

Compatibility note: the bridge targets the *Agent Server* (``agent-server``
package, routes under ``/api``), not the Sandbox Server control plane
(``/api/v1``) and not the archived V0 Local GUI API.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx

from openhands_connect.domain.errors import InvalidInputError
from openhands_connect.domain.models import (
    CredentialState,
    Environment,
    HealthStatus,
    MessageDelivery,
    ProviderAmbiguousWrite,
    ProviderError,
    ProviderInvalidRequest,
    ProviderNotFound,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderTimeout,
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
#: ``events/search`` rejects ``limit`` above 100 with a 422 (verified on
#: Agent Server 1.60.1: "Input should be less than or equal to 100").
MAX_LOG_ENTRIES = 100
#: An authenticated route used to prove the credential is accepted.
#: ``/server_info`` is unauthenticated and answers 200 even for a bad token.
CREDENTIAL_PROBE_PATH = "/api/conversations/count"
#: Readiness route: 200 only after initialization, 503 while starting.
READINESS_PATH = "/ready"

#: Statuses worth another attempt on a call proven safe to repeat. 429 is the
#: documented "run capacity full"; the 5xx set is the gateway/overload family.
#: A plain 500 is *not* retried: it usually means the server is broken, and
#: retrying only adds load.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
#: Exponential backoff, capped. Kept small so a tool call stays interactive.
RETRY_BASE_DELAY_SECONDS: float = 0.25
RETRY_MAX_DELAY_SECONDS: float = 4.0

#: Transport failures that prove the request never reached the server. A write
#: failing this way definitely did *not* happen, so it is a plain failure rather
#: than an ambiguous one.
_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
#: Transport failures where the request may have been delivered and applied.
#: For a non-idempotent write these are ambiguous, never a reported failure.
_AMBIGUOUS_FAILURES = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)

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
        """Report reachability, readiness, *and* whether the credential is accepted.

        ``/server_info`` is unauthenticated: it answers 200 with the version even
        for a wrong key, so it alone cannot tell a working token from a dead one.
        When a credential is configured a second, authenticated request
        (``GET /api/conversations/count``, same ``APIKeyHeader`` scheme as the
        conversation routes) confirms it. ``GET /ready`` then reports whether the
        server has finished initializing (503 while starting).

        The failure modes stay distinct: a rejected token reports
        ``credential_state=REJECTED``, a starting server reports ``ready=False``,
        while a 5xx, timeout, or unexpected status on a probe reports
        ``credential_state=UNKNOWN`` — a broken server must not be reported as a
        bad credential, and none of them is reported as healthy.
        """
        credential = self._credential_provider(environment)
        try:
            async with self._calling(environment) as client:
                response = await self._send(
                    client, environment, "GET", "/server_info", idempotent=True
                )
                payload = _json_object(response, environment)

                if not credential:
                    state = CredentialState.NOT_CONFIGURED
                else:
                    state = await self._probe_credential(client, environment)
                    if state is not CredentialState.ACCEPTED:
                        return HealthStatus(
                            reachable=False,
                            detail=_credential_failure_detail(state),
                            credential_state=state,
                        )
                ready = await self._probe_readiness(client)
                if ready is False:
                    return HealthStatus(
                        reachable=True,
                        ready=False,
                        detail=(
                            "server is reachable but not ready to accept work "
                            "(initializing); retry shortly"
                        ),
                        credential_state=state,
                    )
        except ProviderError as exc:
            # The probe itself can raise here (5xx -> ProviderUnavailable, a
            # timeout, etc.). That is a server problem, not a rejected token.
            return HealthStatus(
                reachable=False,
                detail=exc.message,
                credential_state=(
                    CredentialState.UNKNOWN if credential else CredentialState.NOT_CONFIGURED
                ),
            )
        except httpx.HTTPError as exc:
            return HealthStatus(
                reachable=False,
                detail=_transport_detail(exc),
                credential_state=(
                    CredentialState.UNKNOWN if credential else CredentialState.NOT_CONFIGURED
                ),
            )
        version = payload.get("version")
        return HealthStatus(
            reachable=True,
            ready=ready,
            detail="agent server reachable",
            server_version=str(version) if version is not None else None,
            credential_state=state,
        )

    async def _probe_readiness(self, client: httpx.AsyncClient) -> bool | None:
        """Ask ``/ready`` whether the server finished initializing.

        Returns ``None`` when the route is absent (older server) or answers an
        unexpected status: readiness is then simply not claimed, never assumed.
        """
        try:
            probe = await client.get(READINESS_PATH)
        except httpx.HTTPError:
            return None
        if probe.is_success:
            return True
        if probe.status_code == 503:
            return False
        return None

    async def _probe_credential(
        self, client: httpx.AsyncClient, environment: Environment
    ) -> CredentialState:
        """Ask an authenticated route whether the configured credential works.

        Only 401/403 mean the token is bad. Anything else that is not a success
        is ``UNKNOWN``: a 500 or 429 says nothing about the credential, and
        reporting it as rejected would send the user to rotate a good token.
        """
        probe = await client.get(CREDENTIAL_PROBE_PATH)
        if probe.status_code in (401, 403):
            return CredentialState.REJECTED
        if not probe.is_success:
            return CredentialState.UNKNOWN
        return CredentialState.ACCEPTED

    async def create_task(self, environment: Environment, request: TaskRequest) -> Task:
        # A caller-supplied id makes creation idempotent: repeating the POST with
        # the same id returns the same conversation without re-adding the initial
        # message or restarting a finished run (verified on 1.60.1). That is what
        # makes a retry safe, and what lets an ambiguous outcome name the task id
        # the caller can check instead of retrying blindly.
        conversation_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
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

        async with self._calling(environment) as client:
            if profile and not _looks_like_uuid(profile):
                payload["agent_profile_id"] = await self._resolve_profile_id(
                    client, environment, profile
                )
            response = await self._send(
                client,
                environment,
                "POST",
                "/api/conversations",
                idempotent=True,
                task_id=conversation_id,
                json=payload,
            )
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
        response = await self._send(
            client, environment, "GET", "/api/agent-profiles", idempotent=True
        )
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
        async with self._calling(environment) as client:
            response = await self._send(
                client,
                environment,
                "GET",
                f"/api/conversations/{task_id}",
                idempotent=True,
                task_id=task_id,
                task_id_in_path=True,
            )
            body = _json_object(response, environment)
        return _task_from_payload(body, environment)

    async def get_task_logs(
        self, environment: Environment, task_id: TaskId, *, limit: int = 50
    ) -> TaskLogs:
        bounded = max(1, min(limit, MAX_LOG_ENTRIES))
        async with self._calling(environment) as client:
            response = await self._send(
                client,
                environment,
                "GET",
                f"/api/conversations/{task_id}/events/search",
                idempotent=True,
                task_id=task_id,
                task_id_in_path=True,
                params={"limit": bounded, "sort_order": "TIMESTAMP"},
            )
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
        async with self._calling(environment) as client:
            task_response = await self._send(
                client,
                environment,
                "GET",
                f"/api/conversations/{task_id}",
                idempotent=True,
                task_id=task_id,
                task_id_in_path=True,
            )
            task = _task_from_payload(_json_object(task_response, environment), environment)

            if task.state is not TaskState.COMPLETED:
                return TaskResult(
                    task_id=task_id,
                    environment_id=environment.id,
                    state=task.state,
                    available=False,
                    detail=f"no final response while task state is {task.state.value}",
                )

            response = await self._send(
                client,
                environment,
                "GET",
                f"/api/conversations/{task_id}/agent_final_response",
                idempotent=True,
                task_id=task_id,
                task_id_in_path=True,
            )
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

    async def send_message(
        self, environment: Environment, task_id: TaskId, message: str
    ) -> MessageDelivery:
        """Save a follow-up message, then start the run that consumes it.

        The two steps are separate upstream operations, and upstream can accept
        the save while refusing the run (429 "run capacity is full"). Sending
        with ``run=True`` in one call would collapse both into one status and
        leave the caller unable to tell "not delivered" from "delivered but not
        running" — the exact ambiguity this split removes.

        The save is not retried: a duplicate would add the message twice. It is
        attempted once and, on an undetermined transport failure, reported as an
        ambiguous write rather than a failure.
        """
        async with self._calling(environment) as client:
            await self._send(
                client,
                environment,
                "POST",
                f"/api/conversations/{task_id}/events",
                idempotent=False,
                task_id=task_id,
                task_id_in_path=True,
                json={
                    "role": "user",
                    "content": [{"type": "text", "text": message}],
                    # run=False so a refused run below cannot lose the message.
                    "run": False,
                },
            )
            started = await self._start_run(client, environment, task_id)

        if started is True:
            detail = "message saved and the run started; poll get_task for progress"
        elif started is False:
            detail = (
                "message saved but the run did not start because the server's run "
                "capacity is full; the message is stored. Start it later by sending "
                "another message, or retry after other runs finish."
            )
        else:
            detail = (
                "message saved, but whether the run started could not be confirmed "
                "because the run request failed. The message is stored; check get_task "
                "for progress before sending it again."
            )
        return MessageDelivery(
            task_id=task_id,
            environment_id=environment.id,
            saved=True,
            started=started,
            detail=detail,
        )

    async def _start_run(
        self, client: httpx.AsyncClient, environment: Environment, task_id: TaskId
    ) -> bool | None:
        """Start the run, distinguishing refused / started / undetermined.

        ``POST /run`` is safe to repeat: a second call while already running
        answers 409, which means a run *is* in progress — i.e. started — so 409 is
        accepted rather than raised. A 429 (run capacity full) is a definite "did
        not start" and returns ``False``.

        Anything else — a 5xx, a timeout, or a lost response — cannot be shown
        either way, so it returns ``None`` instead of raising. Raising here would
        discard the fact that the message *was* already saved, and the caller
        would retry the whole send, storing the message twice.
        """
        try:
            await self._send(
                client,
                environment,
                "POST",
                f"/api/conversations/{task_id}/run",
                idempotent=True,
                task_id=task_id,
                task_id_in_path=True,
                ok_statuses=frozenset({409}),
            )
        except ProviderRateLimited:
            return False
        except ProviderError:
            return None
        return True

    async def stop_task(self, environment: Environment, task_id: TaskId) -> StopResult:
        """Pause the task, and say plainly that it was not terminated.

        The Agent Server exposes ``interrupt`` and ``pause``, both of which land
        the conversation in ``paused``; no endpoint terminates a run. Reporting
        ``cancelled=True`` would claim something upstream cannot do, so the
        result keeps ``cancelled=False`` and states the real effect.

        ``interrupt`` is idempotent upstream (a second call answers 200), so it
        is retried on 5xx/timeout like a read.
        """
        async with self._calling(environment) as client:
            await self._send(
                client,
                environment,
                "POST",
                f"/api/conversations/{task_id}/interrupt",
                idempotent=True,
                task_id=task_id,
                task_id_in_path=True,
            )
            status_response = await self._send(
                client,
                environment,
                "GET",
                f"/api/conversations/{task_id}",
                idempotent=True,
                task_id=task_id,
                task_id_in_path=True,
            )
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

    @asynccontextmanager
    async def _calling(self, environment: Environment) -> AsyncIterator[httpx.AsyncClient]:
        """Open a client and translate transport failures into provider errors.

        ``httpx`` errors are not ``ProviderError``, so letting one escape makes
        the tool layer answer ``internal_error``: the caller loses both the cause
        and the retry hint. A refused connection or a timeout is an upstream
        availability problem and is reported as one.
        """
        try:
            async with self._client(environment) as client:
                yield client
        except httpx.HTTPError as exc:
            raise _transport_error(exc, environment, self._redactor_for(environment)) from None

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

    async def _send(
        self,
        client: httpx.AsyncClient,
        environment: Environment,
        method: str,
        path: str,
        *,
        idempotent: bool,
        task_id: str | None = None,
        task_id_in_path: bool = False,
        ok_statuses: frozenset[int] = frozenset(),
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform one request, retrying only what is safe to repeat.

        Retry policy (documented in ``docs/RELIABILITY.md``):

        * A read, or a write the caller has proven idempotent (``idempotent``),
          is retried on a timeout, a connect failure, or a status in
          ``RETRYABLE_STATUSES``, up to ``environment.max_attempts``, with capped
          exponential backoff that honours ``Retry-After``.
        * A non-idempotent write is attempted exactly once. A 4xx proves it was
          rejected, so it is a plain failure; a 5xx or a lost response leaves the
          outcome undetermined, so it raises ``ProviderAmbiguousWrite`` rather
          than a false failure or a false success, and a caller never retries
          into a duplicate.

        ``ok_statuses`` names non-2xx statuses the caller treats as success (e.g.
        409 for "a run is already in progress"); they are returned, not raised.

        Only the exception *type* and fixed phrases reach the message: the
        original ``httpx`` text can name a host, a URL, or a header value.
        """
        attempts = environment.max_attempts if idempotent else 1
        last_transport: httpx.HTTPError | None = None
        response: httpx.Response | None = None

        for attempt in range(attempts):
            retryable_status = False
            retry_after: float | None = None
            # Reset per attempt: a stale response from an earlier attempt must
            # not be what gets reported if a later attempt fails in transport.
            response = None
            try:
                response = await client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                last_transport = exc
                if not idempotent:
                    raise self._write_failure(exc, environment, task_id=task_id) from None
                if not isinstance(exc, _CONNECT_FAILURES + _AMBIGUOUS_FAILURES):
                    raise _transport_error(
                        exc, environment, self._redactor_for(environment)
                    ) from None
            else:
                if response.is_success or response.status_code in ok_statuses:
                    return response
                if task_id_in_path and _is_task_id_parse_error(response):
                    raise _invalid_task_id(task_id, environment, response)
                if not idempotent:
                    # 4xx was rejected before it applied anything; a 5xx cannot
                    # be shown not to have applied, so it is ambiguous.
                    if response.status_code >= 500:
                        raise self._ambiguous_status(response, environment, task_id=task_id)
                    self._raise_for_status(response, environment)
                retryable_status = response.status_code in RETRYABLE_STATUSES
                retry_after = _retry_after_seconds(response)
                if not retryable_status:
                    self._raise_for_status(response, environment)

            if attempt + 1 >= attempts:
                break
            await asyncio.sleep(_backoff_delay(attempt, retry_after))

        if response is not None:
            self._raise_for_status(response, environment)
        if last_transport is not None:
            raise _transport_error(last_transport, environment, self._redactor_for(environment))
        raise ProviderUnavailable(
            f"environment {environment.id!r} did not respond after {attempts} attempts"
        )

    def _ambiguous_status(
        self, response: httpx.Response, environment: Environment, *, task_id: str | None
    ) -> ProviderAmbiguousWrite:
        """A 5xx to a non-idempotent write: the request arrived, the effect is unknown.

        The handler may have applied the change before failing, or a gateway may
        have applied it and then failed to relay the reply. Neither success nor
        failure is provable, so it is reported as ambiguous.
        """
        detail = self._redactor_for(environment).redact(_safe_error_detail(response))
        hint = (
            f" The write was keyed on {task_id!r}; check that id before retrying."
            if task_id
            else " Do not retry blindly: the effect may already have been applied."
        )
        return ProviderAmbiguousWrite(
            f"environment {environment.id!r} returned {response.status_code} for a mutating "
            f"request; it may or may not have been applied ({detail})." + hint,
            task_id=task_id,
        )

    def _write_failure(
        self, exc: httpx.HTTPError, environment: Environment, *, task_id: str | None
    ) -> ProviderError:
        """Classify a non-retryable write's transport failure.

        A refused/timed-out *connection* proves nothing was sent, so it is an
        ordinary failure. A read/write timeout or a reset after the request went
        out leaves the effect undetermined: report that, do not claim failure.
        """
        target = environment.id
        redactor = self._redactor_for(environment)
        if isinstance(exc, _CONNECT_FAILURES) and not isinstance(exc, _AMBIGUOUS_FAILURES):
            return ProviderUnavailable(
                f"environment {target!r} is unreachable: {redactor.redact(_transport_detail(exc))}"
            )
        if isinstance(exc, _AMBIGUOUS_FAILURES):
            hint = (
                f" The write was keyed on {task_id!r}; check that id before retrying."
                if task_id
                else " Do not retry blindly: the effect may already have been applied."
            )
            return ProviderAmbiguousWrite(
                f"environment {target!r} did not confirm the result of a mutating "
                f"request ({type(exc).__name__}); it may or may not have been applied." + hint,
                task_id=task_id,
            )
        return _transport_error(exc, environment, redactor)


def _is_task_id_parse_error(response: httpx.Response) -> bool:
    """Whether a 422 is specifically about the conversation id in the path.

    A 422 on a task-scoped route is not automatically about the id: the message
    body is validated on the same route, and mislabelling a rejected body as a
    malformed task id sends the caller to fix the wrong thing. FastAPI's
    ``RequestValidationError`` names the offending location, so the check looks
    for the path parameter. When the body is not the expected JSON shape the
    check declines to claim it, falling back to a generic invalid-request.
    """
    if response.status_code != 422:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    errors = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, dict):
            continue
        location = error.get("loc")
        if isinstance(location, list) and "path" in location and "conversation_id" in location:
            return True
    return False


def _invalid_task_id(
    task_id: str | None, environment: Environment, response: httpx.Response
) -> InvalidInputError:
    """Report a task id upstream cannot even parse as a caller input error.

    A non-UUID id yields a 422 ``uuid_parsing`` failure, which is a different
    problem from a well-formed id the server does not know (404). Folding both
    into one message would send a caller looking for a deleted task when the id
    was simply malformed.
    """
    detail = Redactor().redact(_safe_error_detail(response))
    return InvalidInputError(
        f"task id {task_id!r} is not valid for environment {environment.id!r}: "
        f"upstream requires a UUID conversation id ({detail})"
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


def _credential_failure_detail(state: CredentialState) -> str:
    """Phrase the health detail by credential state, keeping causes distinct."""
    if state is CredentialState.REJECTED:
        return (
            "server reachable but rejected the credential; check the value of the "
            "configured credential env var"
        )
    # UNKNOWN: the probe could not settle the question. Say so rather than
    # implying the token is bad or that the server is fine.
    return (
        "server reachable but the credential could not be verified; an "
        "authenticated request returned an unexpected status"
    )


def _transport_detail(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    return f"connection failed: {type(exc).__name__}"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` delay in seconds, ignoring the HTTP-date form.

    A date would need clock comparison; treating it as absent falls back to
    exponential backoff, which is bounded and always terminates.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Capped exponential backoff, honouring a ``Retry-After`` hint.

    The hint is clamped to ``RETRY_MAX_DELAY_SECONDS`` so a server asking for a
    long wait cannot stall a tool call; the caller then sees the 429 instead.
    """
    if retry_after is not None:
        return min(retry_after, RETRY_MAX_DELAY_SECONDS)
    # ``2.0 ** attempt`` rather than ``2 ** attempt``: mypy types int**int as
    # Any (a negative exponent would be a float), which would leak Any out.
    return min(RETRY_BASE_DELAY_SECONDS * (2.0**attempt), RETRY_MAX_DELAY_SECONDS)


def _transport_error(
    exc: httpx.HTTPError, environment: Environment, redactor: Redactor
) -> ProviderError:
    """Map a transport failure onto the provider taxonomy.

    Only the exception *type* and a fixed phrase reach the message: the original
    ``httpx`` text can name the host, a URL, or a header value.
    """
    detail = redactor.redact(_transport_detail(exc))
    target = environment.id
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeout(f"environment {target!r} did not respond in time: {detail}")
    return ProviderUnavailable(f"environment {target!r} is unreachable: {detail}")


__all__ = [
    "API_KEY_HEADER",
    "CREDENTIAL_PROBE_PATH",
    "MAX_LOG_ENTRIES",
    "STATE_MAP",
    "OpenHandsAdapter",
    "map_state",
]
