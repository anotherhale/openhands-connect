"""Adapter tests driven through a real HTTP transport.

``httpx.MockTransport`` is the seam: the adapter's own code — URL building,
auth header, payload shape, status mapping, redaction, event summarization —
all runs for real. Only the remote server is substituted, which is exactly the
boundary the adapter exists to isolate.

Every payload shape here was captured from a live OpenHands Agent Server 1.60.1.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest

from openhands_connect.adapters.openhands import (
    API_KEY_HEADER,
    MAX_LOG_ENTRIES,
    STATE_MAP,
    OpenHandsAdapter,
    map_state,
)
from openhands_connect.domain.errors import ConfigurationError
from openhands_connect.domain.models import (
    CredentialState,
    Environment,
    ProviderError,
    ProviderErrorKind,
    ProviderInvalidRequest,
    ProviderNotFound,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnauthorized,
    ProviderUnavailable,
    TaskRequest,
    TaskState,
)
from openhands_connect.redaction import Redactor
from openhands_connect.tests.conftest import make_environment

Handler = Callable[[httpx.Request], httpx.Response]


def adapter_for(
    handler: Handler, environment: Environment, *, credential: str | None = "secret-token"
) -> OpenHandsAdapter:
    return OpenHandsAdapter(
        lambda _env: credential,
        transport=httpx.MockTransport(handler),
        redactor=Redactor((credential,) if credential else ()),
    )


# -- state mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        ("idle", TaskState.DISPATCHED),
        ("running", TaskState.RUNNING),
        ("paused", TaskState.PAUSED),
        ("waiting_for_confirmation", TaskState.WAITING_FOR_CONFIRMATION),
        ("finished", TaskState.COMPLETED),
        ("error", TaskState.FAILED),
        ("stuck", TaskState.FAILED),
        # Deletion is not cancellation; upstream gives no verified signal that a
        # deleted conversation was cancelled, so it must not read as CANCELLED.
        ("deleting", TaskState.UNKNOWN),
    ],
)
def test_documented_upstream_states_map(upstream: str, expected: TaskState) -> None:
    assert map_state(upstream) == expected


@pytest.mark.parametrize("upstream", [None, "", "exploded", "RUNNING", "brand_new_status"])
def test_unknown_upstream_states_are_not_guessed(upstream: str | None) -> None:
    assert map_state(upstream) is TaskState.UNKNOWN


def test_state_map_covers_every_documented_status() -> None:
    """If upstream adds a status, this list is the reminder to map it."""
    documented = {
        "idle",
        "running",
        "paused",
        "waiting_for_confirmation",
        "finished",
        "error",
        "stuck",
        "deleting",
    }
    assert set(STATE_MAP) == documented


# -- health -----------------------------------------------------------------


async def test_health_check_reports_version(environment: Environment) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(
                200, json={"version": "1.60.1", "title": "OpenHands Agent Server"}
            )
        return httpx.Response(200, json=7)

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is True
    assert health.server_version == "1.60.1"


async def test_health_check_sends_the_session_api_key_header(environment: Environment) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(200, json=1)

    await adapter_for(handler, environment).health_check(environment)
    assert seen[API_KEY_HEADER.lower()] == "secret-token"


async def test_health_check_without_credential_sends_no_key_header(
    environment: Environment,
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"version": "1.60.1"})

    await adapter_for(handler, environment, credential=None).health_check(environment)
    assert API_KEY_HEADER.lower() not in seen


async def test_missing_credential_fails_closed_before_sending(environment: Environment) -> None:
    """A configured-but-absent credential must abort, not send anonymously."""
    sent = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, json={"version": "1.60.1"})

    def provider(_env: Environment) -> str | None:
        raise ConfigurationError(
            "environment 'local' requires credential env var OH_TEST_API_KEY, which is not set"
        )

    adapter = OpenHandsAdapter(provider, transport=httpx.MockTransport(handler))
    with pytest.raises(ConfigurationError):
        await adapter.health_check(environment)
    assert sent is False, "no request may be sent when the credential is missing"


async def test_injected_redactor_secrets_survive_the_credential(
    environment: Environment,
) -> None:
    """The adapter adds the credential to the injected set; it must not replace it."""
    other_secret = "operator-configured-secret"
    adapter = OpenHandsAdapter(
        lambda _env: "session-token-value",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                500, json={"detail": f"boom session-token-value and {other_secret}"}
            )
        ),
        redactor=Redactor((other_secret,)),
    )

    with pytest.raises(ProviderUnavailable) as excinfo:
        await adapter.get_task(environment, "task-1")

    message = str(excinfo.value)
    assert "session-token-value" not in message
    assert other_secret not in message


async def test_health_check_reports_rejected_credential(environment: Environment) -> None:
    """A 401 on the authenticated probe means the credential is bad."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(401, json={"detail": "Unauthorized"})

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is False
    assert health.credential_state is CredentialState.REJECTED
    assert "credential" in health.detail


async def test_health_check_reports_an_accepted_credential(environment: Environment) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(200, json=7)

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is True
    assert health.credential_state is CredentialState.ACCEPTED


async def test_health_check_reports_no_credential_as_not_configured(
    environment: Environment,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "1.60.1"})

    health = await adapter_for(handler, environment, credential=None).health_check(environment)

    assert health.reachable is True
    assert health.credential_state is CredentialState.NOT_CONFIGURED


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_probe_server_fault_is_unknown_not_a_rejected_credential(
    environment: Environment, status: int
) -> None:
    """A broken server must not be reported as a bad token.

    Only 401/403 mean the credential is wrong. A 5xx or 429 says nothing about
    it, so the state is UNKNOWN — reporting REJECTED would send the user to
    rotate a perfectly good token.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(status, json={"detail": "server-side problem"})

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is False
    assert health.credential_state is CredentialState.UNKNOWN


async def test_probe_timeout_is_unknown_not_a_rejected_credential(
    environment: Environment,
) -> None:
    """A timeout on the probe is an availability problem, not a bad token."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        raise httpx.ReadTimeout("slow probe")

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is False
    assert health.credential_state is CredentialState.UNKNOWN


async def test_health_check_reports_unreachable_without_leaking_secret(
    environment: Environment,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused for token secret-token")

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is False
    assert "secret-token" not in health.detail


# -- create_task ------------------------------------------------------------

CREATED = {
    "id": "07adb7cc-0118-473d-99c8-5daccd928f82",
    "execution_status": "idle",
    "created_at": "2026-11-27T00:52:36.000000Z",
    "updated_at": "2026-11-27T00:52:36.000000Z",
    "title": "do the thing",
}


async def test_create_task_sends_verified_payload(environment: Environment) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/conversations"
        captured.update(json.loads(request.content))
        return httpx.Response(201, json=CREATED)

    from openhands_connect.domain.models import TaskRequest

    task = await adapter_for(handler, environment).create_task(
        environment, TaskRequest(prompt="do the thing", workspace="/tmp/ws", max_iterations=8)
    )

    assert captured["workspace"] == {"kind": "LocalWorkspace", "working_dir": "/tmp/ws"}
    assert captured["initial_message"] == {
        "role": "user",
        "content": [{"type": "text", "text": "do the thing"}],
        "run": True,
    }
    assert captured["max_iterations"] == 8
    assert captured["agent_profile_id"] == "11111111-2222-3333-4444-555555555555"
    assert task.id == CREATED["id"]
    assert task.state is TaskState.DISPATCHED
    assert task.provider_state == "idle"
    assert task.environment_id == "local"


async def test_create_task_uses_agent_settings_when_no_profile_configured() -> None:
    """Upstream requires one of agent/agent_settings/agent_profile_id."""
    environment = make_environment(default_agent_profile_id=None)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json=CREATED)

    from openhands_connect.domain.models import TaskRequest

    await adapter_for(handler, environment).create_task(
        environment,
        TaskRequest(
            prompt="x",
            agent_settings={"llm": {"model": "deepseek-v4.1-flash"}},
        ),
    )
    assert "agent_profile_id" not in captured
    assert captured["agent_settings"] == {"llm": {"model": "deepseek-v4.1-flash"}}


async def test_create_task_without_any_agent_source_fails_before_sending() -> None:
    """A 422 from upstream is worse than a local, explanatory error."""
    environment = make_environment(default_agent_profile_id=None)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json=CREATED)

    from openhands_connect.domain.errors import InvalidInputError
    from openhands_connect.domain.models import TaskRequest

    with pytest.raises(InvalidInputError, match="no agent configured"):
        await adapter_for(handler, environment).create_task(environment, TaskRequest(prompt="x"))

    assert sent == [], "no request should reach the server without an agent source"


async def test_create_task_resolves_a_profile_name_to_its_uuid() -> None:
    """A config file naturally holds a name; upstream only accepts a UUID.

    Verified against Agent Server 1.60.1: posting ``"autonomous"`` as
    ``agent_profile_id`` returns 422 ``uuid_parsing``.
    """
    profile_uuid = "76877700-937e-4cab-9cf5-7aa2d75f9573"
    environment = make_environment(default_agent_profile_id="autonomous")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={
                    "profiles": [
                        {"id": profile_uuid, "name": "autonomous"},
                        {"id": "d72f1904-0cfa-4b98-960c-f02274a7efb1", "name": "default"},
                    ]
                },
            )
        assert json.loads(request.content)["agent_profile_id"] == profile_uuid
        return httpx.Response(201, json=CREATED)

    from openhands_connect.domain.models import TaskRequest

    await adapter_for(handler, environment).create_task(environment, TaskRequest(prompt="x"))

    assert seen == ["/api/agent-profiles", "/api/conversations"]


async def test_create_task_skips_resolution_for_a_uuid_already_configured() -> None:
    profile_uuid = "76877700-937e-4cab-9cf5-7aa2d75f9573"
    environment = make_environment(default_agent_profile_id=profile_uuid)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(201, json=CREATED)

    from openhands_connect.domain.models import TaskRequest

    await adapter_for(handler, environment).create_task(environment, TaskRequest(prompt="x"))

    assert seen == ["/api/conversations"], "a UUID needs no lookup"


async def test_create_task_reports_available_profiles_when_the_name_is_unknown() -> None:
    environment = make_environment(default_agent_profile_id="typo-profile")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "profiles": [{"id": "abc", "name": "autonomous"}, {"id": "def", "name": "default"}]
            },
        )

    from openhands_connect.domain.errors import InvalidInputError
    from openhands_connect.domain.models import TaskRequest

    with pytest.raises(InvalidInputError) as excinfo:
        await adapter_for(handler, environment).create_task(environment, TaskRequest(prompt="x"))

    message = str(excinfo.value)
    assert "typo-profile" in message
    assert "autonomous" in message and "default" in message


async def test_create_task_prefers_explicit_profile_over_environment_default() -> None:
    explicit = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    environment = make_environment(default_agent_profile_id="env-default")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json=CREATED)

    from openhands_connect.domain.models import TaskRequest

    await adapter_for(handler, environment).create_task(
        environment, TaskRequest(prompt="x", agent_profile_id=explicit)
    )
    assert captured["agent_profile_id"] == explicit


async def test_create_task_prompt_is_not_echoed_in_errors(environment: Environment) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "agent_profile_id is required"})

    from openhands_connect.domain.models import TaskRequest

    with pytest.raises(ProviderInvalidRequest) as excinfo:
        await adapter_for(handler, environment).create_task(
            environment, TaskRequest(prompt="super-secret-prompt")
        )
    assert "super-secret-prompt" not in str(excinfo.value)


# -- get_task ---------------------------------------------------------------


async def test_get_task_maps_unrecognized_status_to_unknown(environment: Environment) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "t1", "execution_status": "teleporting"})

    task = await adapter_for(handler, environment).get_task(environment, "t1")

    assert task.state is TaskState.UNKNOWN
    assert task.provider_state == "teleporting"
    assert task.detail is not None
    assert "teleporting" in task.detail


async def test_get_task_without_status_is_unknown(environment: Environment) -> None:
    handler: Handler = lambda _request: httpx.Response(200, json={"id": "t1"})
    task = await adapter_for(handler, environment).get_task(environment, "t1")
    assert task.state is TaskState.UNKNOWN
    assert task.detail is not None


async def test_get_task_404_raises_not_found(environment: Environment) -> None:
    handler: Handler = lambda _request: httpx.Response(404, json={"detail": "Not Found"})
    with pytest.raises(ProviderNotFound):
        await adapter_for(handler, environment).get_task(environment, "missing")


async def test_missing_conversation_id_is_a_protocol_error(environment: Environment) -> None:
    handler: Handler = lambda _request: httpx.Response(200, json={"execution_status": "idle"})
    with pytest.raises(ProviderProtocolError):
        await adapter_for(handler, environment).get_task(environment, "t1")


async def test_non_json_response_is_a_protocol_error(environment: Environment) -> None:
    handler: Handler = lambda _request: httpx.Response(200, text="<html>nope</html>")
    with pytest.raises(ProviderProtocolError):
        await adapter_for(handler, environment).get_task(environment, "t1")


async def test_json_array_response_is_a_protocol_error(environment: Environment) -> None:
    handler: Handler = lambda _request: httpx.Response(200, json=[1, 2, 3])
    with pytest.raises(ProviderProtocolError):
        await adapter_for(handler, environment).get_task(environment, "t1")


# -- error mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderUnauthorized),
        (403, ProviderUnauthorized),
        (404, ProviderNotFound),
        (422, ProviderInvalidRequest),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
    ],
)
async def test_http_status_maps_to_provider_error(
    environment: Environment, status: int, expected: type[Exception]
) -> None:
    handler: Handler = lambda _request: httpx.Response(status, json={"detail": "nope"})
    with pytest.raises(expected):
        await adapter_for(handler, environment).get_task(environment, "t1")


async def test_422_detail_list_is_summarized(environment: Environment) -> None:
    payload = {
        "detail": [
            {
                "type": "uuid_parsing",
                "loc": ["path", "conversation_id"],
                "msg": "Input should be a valid UUID",
            }
        ]
    }
    handler: Handler = lambda _request: httpx.Response(422, json=payload)

    with pytest.raises(ProviderInvalidRequest) as excinfo:
        await adapter_for(handler, environment).get_task(environment, "t1")
    assert "Input should be a valid UUID" in str(excinfo.value)


async def test_error_message_never_contains_the_credential(environment: Environment) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json={"detail": "upstream rejected X-Session-API-Key: secret-token"}
        )

    with pytest.raises(ProviderUnavailable) as excinfo:
        await adapter_for(handler, environment).get_task(environment, "t1")

    assert "secret-token" not in str(excinfo.value)
    assert excinfo.value.kind is ProviderErrorKind.UNAVAILABLE
    assert excinfo.value.retryable is True


# -- logs -------------------------------------------------------------------

LOG_PAGE = {
    "items": [
        {
            "id": "e1",
            "timestamp": "2026-11-27T00:52:40.948467",
            "source": "agent",
            "kind": "MessageEvent",
            "llm_message": {
                "role": "assistant",
                "content": [{"cache_prompt": False, "type": "text", "text": "PONG"}],
            },
        },
        {
            "id": "e2",
            "timestamp": "2026-11-27T00:52:40.949268",
            "source": "environment",
            "kind": "ConversationStateUpdateEvent",
            "key": "execution_status",
            "value": "finished",
        },
        {
            "id": "e3",
            "timestamp": "2026-11-27T00:52:41.000000",
            "source": "agent",
            "kind": "ActionEvent",
            "thought": [{"type": "text", "text": "I will run ls"}],
            "action": {"tool_name": "terminal"},
        },
        {
            "id": "e4",
            "timestamp": "2026-11-27T00:52:42.000000",
            "source": "agent",
            "kind": "AgentErrorEvent",
            "error": "command failed",
        },
    ],
    "next_page_id": None,
}


async def test_logs_are_summarized_per_event_kind(environment: Environment) -> None:
    handler: Handler = lambda _request: httpx.Response(200, json=LOG_PAGE)
    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1")

    texts = [entry.text for entry in logs.entries]
    assert texts == [
        "assistant: PONG",
        "state execution_status=finished",
        "action terminal: I will run ls",
        "agent error: command failed",
    ]
    assert logs.entries[0].kind == "MessageEvent"
    assert logs.truncated is False


async def test_logs_request_bounds_limit(environment: Environment) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"items": [], "next_page_id": None})

    await adapter_for(handler, environment).get_task_logs(environment, "t1", limit=5000)
    assert captured["limit"] == "100"


async def test_logs_limit_matches_the_upstream_maximum(environment: Environment) -> None:
    """``events/search`` rejects ``limit`` above 100 with a 422.

    Verified against Agent Server 1.60.1: ``limit=200`` fails with
    ``Input should be less than or equal to 100``. The tool schema advertised
    1-200, so the largest value a caller could ask for was always an error.
    """
    assert MAX_LOG_ENTRIES == 100

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"items": [], "next_page_id": None})

    await adapter_for(handler, environment).get_task_logs(environment, "t1", limit=MAX_LOG_ENTRIES)
    assert captured["limit"] == "100"


async def test_logs_truncate_long_event_text(environment: Environment) -> None:
    huge = "x" * 10_000
    page = {
        "items": [
            {
                "id": "e1",
                "kind": "MessageEvent",
                "source": "agent",
                "llm_message": {"role": "assistant", "content": [{"type": "text", "text": huge}]},
            }
        ]
    }
    handler: Handler = lambda _request: httpx.Response(200, json=page)

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1")
    text = logs.entries[0].text
    assert len(text) < 2_100
    assert text.endswith("…[truncated]")


async def test_logs_redact_credentials_from_text(environment: Environment) -> None:
    page = {
        "items": [
            {
                "id": "e1",
                "kind": "MessageEvent",
                "source": "agent",
                "llm_message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "using key secret-token to authenticate"}],
                },
            }
        ]
    }
    handler: Handler = lambda _request: httpx.Response(200, json=page)

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1")
    assert "secret-token" not in logs.entries[0].text


async def test_unrecognized_event_kind_still_appears(environment: Environment) -> None:
    page = {"items": [{"id": "e1", "kind": "SomeFutureEvent", "source": "agent"}]}
    handler: Handler = lambda _request: httpx.Response(200, json=page)

    logs = await adapter_for(handler, environment).get_task_logs(environment, "t1")
    assert logs.entries[0].text == "SomeFutureEvent"


async def test_events_without_items_is_a_protocol_error(environment: Environment) -> None:
    handler: Handler = lambda _request: httpx.Response(200, json={"unexpected": True})
    with pytest.raises(ProviderProtocolError):
        await adapter_for(handler, environment).get_task_logs(environment, "t1")


# -- result -----------------------------------------------------------------


async def test_result_requires_completed_state(environment: Environment) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent_final_response"):
            raise AssertionError("must not fetch a final response for a running task")
        return httpx.Response(200, json={"id": "t1", "execution_status": "running"})

    result = await adapter_for(handler, environment).get_task_result(environment, "t1")

    assert result.available is False
    assert result.state is TaskState.RUNNING
    assert result.detail is not None


async def test_result_returns_final_response_when_completed(environment: Environment) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent_final_response"):
            return httpx.Response(200, json={"response": "PONG"})
        return httpx.Response(200, json={"id": "t1", "execution_status": "finished"})

    result = await adapter_for(handler, environment).get_task_result(environment, "t1")

    assert result.available is True
    assert result.summary == "PONG"
    assert result.state is TaskState.COMPLETED


async def test_empty_final_response_is_not_reported_as_a_result(
    environment: Environment,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent_final_response"):
            return httpx.Response(200, json={"response": ""})
        return httpx.Response(200, json={"id": "t1", "execution_status": "finished"})

    result = await adapter_for(handler, environment).get_task_result(environment, "t1")

    assert result.available is False
    assert result.summary is None
    assert result.detail is not None


# -- messaging and stop -----------------------------------------------------


async def test_send_message_posts_a_running_user_message(environment: Environment) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations/t1/events"
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    await adapter_for(handler, environment).send_message(environment, "t1", "keep going")

    assert captured == {
        "role": "user",
        "content": [{"type": "text", "text": "keep going"}],
        "run": True,
    }


async def test_stop_task_does_not_claim_cancellation(environment: Environment) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/interrupt"):
            assert request.method == "POST"
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"id": "t1", "execution_status": "paused"})

    result = await adapter_for(handler, environment).stop_task(environment, "t1")

    assert result.cancelled is False
    assert result.state is TaskState.PAUSED
    assert "does not support terminating" in result.detail


# -- routing ----------------------------------------------------------------


async def test_adapter_uses_the_environment_base_url() -> None:
    first = make_environment("first", base_url="http://127.0.0.1:18000")
    second = make_environment("second", base_url="http://127.0.0.1:19000")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}:{request.url.port}")
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(200, json=1)

    adapter = adapter_for(handler, first)
    await adapter.health_check(first)
    await adapter.health_check(second)

    # Each health check makes two calls: /server_info, then the credential probe.
    assert seen == [
        "127.0.0.1:18000",
        "127.0.0.1:18000",
        "127.0.0.1:19000",
        "127.0.0.1:19000",
    ]


async def test_timeout_is_reported_as_unreachable(environment: Environment) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    health = await adapter_for(handler, environment).health_check(environment)
    assert health.reachable is False
    assert "timed out" in health.detail


# -- health must actually verify the credential -----------------------------


async def test_health_check_reports_a_rejected_credential_as_unreachable(
    environment: Environment,
) -> None:
    """A server that rejects the token is not a usable target.

    Verified against Agent Server 1.60.1: ``GET /server_info`` answers 200 even
    with a wrong key (it is unauthenticated), so a health check that stops there
    reports a broken credential as healthy.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(401, json={"detail": "Unauthorized"})

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is False
    assert health.server_version is None
    assert "credential" in health.detail
    assert "secret-token" not in health.detail


async def test_health_check_probes_an_authenticated_endpoint(
    environment: Environment,
) -> None:
    """The credential is only checked by a route that requires it."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(200, json=3)

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is True
    assert health.server_version == "1.60.1"
    assert "/api/conversations/count" in paths


async def test_health_check_skips_the_credential_probe_when_none_is_configured(
    environment: Environment,
) -> None:
    """An unauthenticated server must not be reported as broken."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(401, json={"detail": "Unauthorized"})

    health = await adapter_for(handler, environment, credential=None).health_check(environment)

    assert health.reachable is True
    assert paths == ["/server_info"]


async def test_health_check_reports_a_broken_credential_probe_as_unreachable(
    environment: Environment,
) -> None:
    """A 5xx on the authenticated probe is a real failure, not a health pass."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        return httpx.Response(503, json={"detail": "not ready"})

    health = await adapter_for(handler, environment).health_check(environment)

    assert health.reachable is False
    assert health.server_version is None


# -- transport failures must not escape as raw httpx exceptions --------------


@pytest.mark.parametrize(
    "operation",
    ["get_task", "get_task_logs", "get_task_result", "send_message", "stop_task", "create_task"],
)
async def test_transport_failure_never_escapes_as_an_httpx_error(
    environment: Environment, operation: str
) -> None:
    """A refused connection or a timeout must map into the provider taxonomy.

    An escaping ``httpx.ConnectError`` is not a ``ProviderError``, so the tool
    layer can only answer ``internal_error`` — the caller loses both the cause
    and the ``retryable`` hint.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = adapter_for(handler, environment)
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "get_task": lambda: adapter.get_task(environment, "t1"),
        "get_task_logs": lambda: adapter.get_task_logs(environment, "t1"),
        "get_task_result": lambda: adapter.get_task_result(environment, "t1"),
        "send_message": lambda: adapter.send_message(environment, "t1", "hi"),
        "stop_task": lambda: adapter.stop_task(environment, "t1"),
        "create_task": lambda: adapter.create_task(environment, TaskRequest(prompt="hi")),
    }

    with pytest.raises(ProviderError) as excinfo:
        await calls[operation]()

    assert not isinstance(excinfo.value, ProviderProtocolError)
    assert "connection refused" not in str(excinfo.value)
    assert "secret-token" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("refused"), ProviderUnavailable),
        (httpx.ReadTimeout("slow"), ProviderTimeout),
        (httpx.ConnectTimeout("slow"), ProviderTimeout),
    ],
)
async def test_transport_errors_map_to_the_right_provider_error(
    environment: Environment, error: Exception, expected: type[Exception]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(expected) as excinfo:
        await adapter_for(handler, environment).get_task(environment, "t1")
    assert getattr(excinfo.value, "retryable", False) is True


async def test_transport_failure_detail_does_not_leak_the_credential(
    environment: Environment,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not reach host while using secret-token")

    with pytest.raises(ProviderUnavailable) as excinfo:
        await adapter_for(handler, environment).get_task(environment, "t1")
    assert "secret-token" not in str(excinfo.value)
