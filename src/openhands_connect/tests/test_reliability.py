"""Failure semantics and bounded-retry behaviour of the Agent Server adapter.

Every test here encodes a behaviour verified against Agent Server 1.60.1 (see
``docs/RELIABILITY.md`` for the probe results). They run on ``MockTransport``, so
they need no live server, and the ``_no_retry_sleep`` fixture removes only the
backoff *wait*, never the retry decisions.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from openhands_connect.adapters import openhands as adapter_module
from openhands_connect.adapters.openhands import (
    RETRY_MAX_DELAY_SECONDS,
    OpenHandsAdapter,
    _backoff_delay,
    _retry_after_seconds,
)
from openhands_connect.domain.errors import InvalidInputError
from openhands_connect.domain.models import (
    Environment,
    ProviderAmbiguousWrite,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnauthorized,
    ProviderUnavailable,
    TaskRequest,
)
from openhands_connect.tests.conftest import make_environment

Handler = Any


def make_adapter(handler: Handler, credential: str | None = "secret-token") -> OpenHandsAdapter:
    return OpenHandsAdapter(
        transport=httpx.MockTransport(handler),
        credential_provider=lambda _env: credential,
    )


def make_env(**overrides: Any) -> Environment:
    return make_environment(**overrides)


@pytest.fixture
def real_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the real backoff constants for the pure-helper tests.

    The suite-wide ``_no_retry_sleep`` fixture zeroes them so retry tests do not
    sleep; the tests that assert on the delay *values* need the real ones back.
    """
    monkeypatch.setattr(adapter_module, "RETRY_BASE_DELAY_SECONDS", 0.25)
    monkeypatch.setattr(adapter_module, "RETRY_MAX_DELAY_SECONDS", 4.0)


# -- bounded retries on reads ------------------------------------------------


async def test_read_retries_a_retryable_status_then_succeeds() -> None:
    """A 503 is transient, so a read is retried and the success returned."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503, json={"detail": "starting"})
        return httpx.Response(200, json={"id": "t1", "execution_status": "idle"})

    task = await make_adapter(handler).get_task(make_env(), "t1")

    assert task.id == "t1"
    assert len(calls) == 2


async def test_read_gives_up_after_max_attempts() -> None:
    """The bound is real: a permanently failing read stops, it does not loop."""
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(503, json={"detail": "still starting"})

    with pytest.raises(ProviderUnavailable):
        await make_adapter(handler).get_task(make_env(max_attempts=4), "t1")

    assert len(calls) == 4


async def test_max_attempts_of_one_disables_retries() -> None:
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(503, json={"detail": "nope"})

    with pytest.raises(ProviderUnavailable):
        await make_adapter(handler).get_task(make_env(max_attempts=1), "t1")

    assert len(calls) == 1


async def test_a_plain_500_is_not_retried() -> None:
    """500 means "server is broken"; retrying only adds load.

    Only the gateway/overload statuses are treated as transient.
    """
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(ProviderUnavailable):
        await make_adapter(handler).get_task(make_env(max_attempts=3), "t1")

    assert len(calls) == 1


async def test_read_retries_a_timeout() -> None:
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"id": "t1", "execution_status": "idle"})

    task = await make_adapter(handler).get_task(make_env(), "t1")
    assert task.id == "t1"
    assert len(calls) == 2


async def test_a_4xx_is_never_retried() -> None:
    """A rejected request will be rejected again; retrying wastes the budget."""
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(401, json={"detail": "bad key"})

    with pytest.raises(ProviderUnauthorized):
        await make_adapter(handler).get_task(make_env(), "t1")

    assert len(calls) == 1


# -- retry safety on writes --------------------------------------------------


async def test_create_task_is_retried_because_it_is_idempotent() -> None:
    """Creation carries a caller-supplied id, so a retry cannot duplicate it."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"detail": "starting"})
        return httpx.Response(200, json={"id": bodies[0]["conversation_id"]})

    env = make_env(default_agent_profile_id="00000000-0000-0000-0000-000000000001")
    task = await make_adapter(handler).create_task(env, TaskRequest(prompt="do work"))

    assert len(bodies) == 2
    # Same id on both attempts is what makes the retry safe.
    assert bodies[0]["conversation_id"] == bodies[1]["conversation_id"] == task.id


async def test_create_task_generates_a_uuid_conversation_id() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": captured["conversation_id"]})

    env = make_env(default_agent_profile_id="00000000-0000-0000-0000-000000000001")
    await make_adapter(handler).create_task(env, TaskRequest(prompt="do work"))

    import uuid

    uuid.UUID(str(captured["conversation_id"]))  # raises if not a UUID


async def test_message_save_is_not_retried() -> None:
    """Retrying the save would store the message twice.

    A 503 is ambiguous for a non-idempotent write (the handler may have stored
    it before failing), so it is reported as such — but the key assertion is the
    request count: exactly one attempt.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, json={"detail": "starting"})

    with pytest.raises(ProviderAmbiguousWrite):
        await make_adapter(handler).send_message(make_env(), "t1", "hi")

    assert calls == ["/api/conversations/t1/events"]


async def test_ambiguous_write_is_not_reported_as_a_plain_failure() -> None:
    """A read timeout on a write means the effect may already be applied.

    Reporting a plain failure would invite a retry that duplicates the message,
    so the adapter raises the dedicated ambiguous error, naming the task id.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost")

    with pytest.raises(ProviderAmbiguousWrite) as excinfo:
        await make_adapter(handler).send_message(make_env(), "t1", "hi")

    assert excinfo.value.retryable is False
    assert excinfo.value.task_id == "t1"
    assert "t1" in str(excinfo.value)


async def test_a_5xx_to_a_write_is_ambiguous_not_a_plain_failure() -> None:
    """The handler may have stored the message before failing to reply."""
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(500, json={"detail": "internal error"})

    with pytest.raises(ProviderAmbiguousWrite) as excinfo:
        await make_adapter(handler).send_message(make_env(), "t1", "hi")

    assert excinfo.value.task_id == "t1"
    assert len(calls) == 1


async def test_a_4xx_to_a_write_is_a_plain_failure() -> None:
    """A 4xx proves the save was rejected, so nothing was stored."""
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(422, json={"detail": "empty message"})

    with pytest.raises(ProviderInvalidRequest) as excinfo:
        await make_adapter(handler).send_message(make_env(), "t1", "hi")

    assert not isinstance(excinfo.value, ProviderAmbiguousWrite)
    assert len(calls) == 1


async def test_connect_failure_on_a_write_is_a_plain_failure() -> None:
    """A refused connection proves the write never happened, so it is not ambiguous."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderUnavailable) as excinfo:
        await make_adapter(handler).send_message(make_env(), "t1", "hi")

    assert not isinstance(excinfo.value, ProviderAmbiguousWrite)


# -- not-ready and lifecycle semantics ---------------------------------------


async def test_health_reports_not_ready_separately_from_reachable() -> None:
    """A server up but still initializing must not be reported as ready."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        if request.url.path == "/ready":
            return httpx.Response(503, json={"detail": "initializing"})
        return httpx.Response(200, json=3)

    health = await make_adapter(handler).health_check(make_env())

    assert health.reachable is True
    assert health.ready is False
    assert "not ready" in health.detail


async def test_health_reports_ready_when_the_server_is_ready() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        if request.url.path == "/ready":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json=3)

    health = await make_adapter(handler).health_check(make_env())

    assert health.reachable is True
    assert health.ready is True


async def test_readiness_is_unknown_when_the_route_is_absent() -> None:
    """An older server without /ready must not be called unready."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.0.0"})
        if request.url.path == "/ready":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json=3)

    health = await make_adapter(handler).health_check(make_env())

    assert health.reachable is True
    assert health.ready is None


async def test_run_already_running_409_counts_as_started() -> None:
    """409 on /run means a run is in progress, which is the outcome we want."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/run"):
            return httpx.Response(409, json={"detail": "Conversation already running"})
        return httpx.Response(200, json={"success": True})

    delivery = await make_adapter(handler).send_message(make_env(), "t1", "hi")

    assert delivery.saved is True
    assert delivery.started is True


async def test_stop_reports_paused_not_cancelled() -> None:
    """Upstream cannot terminate; the result must not claim it did."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/interrupt"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"id": "t1", "execution_status": "paused"})

    result = await make_adapter(handler).stop_task(make_env(), "t1")

    assert result.cancelled is False
    assert "does not support terminating" in result.detail


# -- error classification ----------------------------------------------------


async def test_timeout_exhausting_retries_is_reported_as_a_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderTimeout):
        await make_adapter(handler).get_task(make_env(max_attempts=2), "t1")


async def test_malformed_task_id_is_invalid_input_not_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "type": "uuid_parsing",
                        "loc": ["path", "conversation_id"],
                        "msg": "Input should be a valid UUID",
                    }
                ]
            },
        )

    with pytest.raises(InvalidInputError):
        await make_adapter(handler).get_task(make_env(), "not-a-uuid")


async def test_a_rejected_message_body_is_not_blamed_on_the_task_id() -> None:
    """A 422 about the body must stay a body error, not become "bad task id"."""
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "type": "string_too_short",
                        "loc": ["body", "content", 0, "text"],
                        "msg": "String should have at least 1 character",
                    }
                ]
            },
        )

    with pytest.raises(ProviderInvalidRequest) as excinfo:
        await make_adapter(handler).send_message(make_env(), "t1", "")

    assert "not valid for environment" not in str(excinfo.value)
    assert len(calls) == 1


async def test_rate_limit_surfaces_after_retries_are_spent() -> None:
    calls: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append("x")
        return httpx.Response(429, json={"detail": "capacity full"})

    with pytest.raises(ProviderRateLimited):
        await make_adapter(handler).get_task(make_env(max_attempts=2), "t1")

    assert len(calls) == 2


async def test_create_without_any_agent_source_is_a_local_input_error() -> None:
    """Upstream 422s without an agent source; catch it before spending a request."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made")

    with pytest.raises(InvalidInputError):
        await make_adapter(handler).create_task(
            make_env(default_agent_profile_id=None), TaskRequest(prompt="x")
        )


async def test_422_from_create_stays_a_provider_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad payload"})

    env = make_env(default_agent_profile_id="00000000-0000-0000-0000-000000000001")
    with pytest.raises(ProviderInvalidRequest):
        await make_adapter(handler).create_task(env, TaskRequest(prompt="x"))


# -- backoff helpers ---------------------------------------------------------


@pytest.mark.usefixtures("real_backoff")
def test_backoff_grows_and_is_capped() -> None:
    delays = [_backoff_delay(attempt, None) for attempt in range(6)]
    assert delays == sorted(delays)
    assert max(delays) == RETRY_MAX_DELAY_SECONDS


@pytest.mark.usefixtures("real_backoff")
def test_retry_after_is_honoured_but_clamped() -> None:
    assert _backoff_delay(0, 1.5) == 1.5
    assert _backoff_delay(0, 10_000) == RETRY_MAX_DELAY_SECONDS


def test_retry_after_parses_seconds_and_ignores_dates() -> None:
    def response(headers: dict[str, str]) -> httpx.Response:
        return httpx.Response(429, headers=headers, json={})

    assert _retry_after_seconds(response({"retry-after": "2"})) == 2.0
    assert _retry_after_seconds(response({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})) is None
    assert _retry_after_seconds(response({})) is None
