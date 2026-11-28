"""MCP surface tests.

These drive the real ``MCPServer`` through ``list_tools``/``call_tool``, so tool
registration, argument schemas, request validation, error shaping and the
service/policy/audit path all execute for real. Only the remote HTTP server is
substituted.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from openhands_connect.adapters.openhands import OpenHandsAdapter
from openhands_connect.domain.models import (
    EXECUTION_OPERATIONS,
    READ_OPERATIONS,
    CredentialState,
    Environment,
    ProviderAmbiguousWrite,
    ProviderUnavailable,
    TaskLogEntry,
)
from openhands_connect.domain.registry import EnvironmentRegistry
from openhands_connect.mcp_server.server import build_server
from openhands_connect.policy import PolicyEngine
from openhands_connect.service import ConnectService
from openhands_connect.tests.conftest import make_environment
from openhands_connect.tests.fakes import FakeProvider

EXPECTED_TOOLS = {
    "list_environments",
    "check_environment",
    "create_task",
    "get_task",
    "get_task_logs",
    "send_task_message",
    "stop_task",
    "get_task_result",
}


@pytest.fixture
def server(service: ConnectService) -> Any:
    return build_server(service)


def payload(result: Any) -> dict[str, Any]:
    """Extract the JSON object a tool returned."""
    return json.loads(result.content[0].text)


async def test_all_documented_tools_are_registered(server) -> None:
    tools = {tool.name for tool in await server.list_tools()}
    assert tools == EXPECTED_TOOLS


async def test_every_tool_has_a_useful_description(server) -> None:
    for tool in await server.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 40, f"{tool.name} description is too terse"


async def test_execution_tools_warn_about_side_effects(server) -> None:
    """A caller must be told that execution can modify files and run commands."""
    by_name = {tool.name: tool for tool in await server.list_tools()}
    for name in ("create_task", "send_task_message"):
        description = by_name[name].description.lower()
        assert "modify files" in description or "file changes" in description
        assert "commands" in description


async def test_stop_task_description_states_the_limitation(server) -> None:
    by_name = {tool.name: tool for tool in await server.list_tools()}
    description = by_name["stop_task"].description.lower()
    assert "no" in description and "terminate" in description
    assert "paused" in description
    # Interruption pauses the run; it must not read as a permanent kill.
    assert "resume" in description or "resumable" in description


async def test_list_environments_returns_sanitized_metadata(server) -> None:
    result = await server.call_tool("list_environments", {})
    body = payload(result)

    assert body["count"] == 1
    environment = body["environments"][0]
    assert environment["id"] == "local"
    assert environment["provider"] == "openhands"
    assert "create_task" in environment["allowed_operations"]
    # The credential reference must not be exposed to a client.
    assert "credential_env_var" not in environment
    assert "OH_TEST_API_KEY" not in json.dumps(body)


async def test_environment_id_is_required_for_execution(server) -> None:
    with pytest.raises(Exception, match="environment_id|validation|required"):
        await server.call_tool("create_task", {"prompt": "hello"})


async def test_invalid_environment_id_shape_is_rejected_by_schema(server) -> None:
    with pytest.raises(Exception, match="validation|pattern|String should match"):
        await server.call_tool("get_task", {"environment_id": "Not Valid!", "task_id": "t1"})


async def test_empty_prompt_is_rejected(server) -> None:
    with pytest.raises(Exception, match="validation|at least 1 character"):
        await server.call_tool("create_task", {"environment_id": "local", "prompt": ""})


async def test_oversized_prompt_is_rejected(server) -> None:
    with pytest.raises(Exception, match="validation|at most 20000"):
        await server.call_tool("create_task", {"environment_id": "local", "prompt": "x" * 20_001})


async def test_log_limit_is_bounded(server) -> None:
    with pytest.raises(Exception, match="validation|less than or equal"):
        await server.call_tool(
            "get_task_logs", {"environment_id": "local", "task_id": "t1", "limit": 5000}
        )


async def test_unknown_environment_returns_a_structured_error(server) -> None:
    result = await server.call_tool("get_task", {"environment_id": "ghost", "task_id": "t1"})
    body = payload(result)

    assert body["error"] == "unknown_environment"
    assert "ghost" in body["message"]


async def test_policy_denial_returns_a_structured_error() -> None:
    environment = make_environment("locked", allowed=frozenset(READ_OPERATIONS))
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=FakeProvider(),
            policy=PolicyEngine(),
        )
    )

    result = await server.call_tool(
        "create_task", {"environment_id": "locked", "prompt": "run tests"}
    )
    body = payload(result)

    assert body["error"] == "policy_denied"
    assert "not permitted" in body["message"]


async def test_create_task_returns_a_task_id(server) -> None:
    result = await server.call_tool(
        "create_task", {"environment_id": "local", "prompt": "fix the bug"}
    )
    body = payload(result)

    assert body["task_id"] == "task-0001"
    assert body["environment_id"] == "local"
    assert body["state"] == "running"


async def test_get_task_exposes_both_normalized_and_provider_state(server) -> None:
    result = await server.call_tool("get_task", {"environment_id": "local", "task_id": "t1"})
    body = payload(result)

    assert body["state"] == "running"
    assert body["provider_state"] == "running"


async def test_provider_failure_is_shaped_without_a_traceback(environment) -> None:
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=FakeProvider(fail_with=ProviderUnavailable("upstream exploded")),
            policy=PolicyEngine(),
        )
    )

    result = await server.call_tool("get_task", {"environment_id": "local", "task_id": "t1"})
    body = payload(result)

    assert body["error"] == "provider_unavailable"
    assert body["retryable"] is True
    assert "Traceback" not in json.dumps(body)


async def test_ambiguous_write_is_reported_as_not_retryable(environment) -> None:
    """A write whose effect is unknown must not invite a blind retry."""
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=FakeProvider(
                fail_with=ProviderAmbiguousWrite("may or may not have been applied")
            ),
            policy=PolicyEngine(),
        )
    )

    result = await server.call_tool(
        "send_task_message",
        {"environment_id": "local", "task_id": "t1", "message": "hi"},
    )
    body = payload(result)

    assert body["error"] == "provider_ambiguous"
    assert body["retryable"] is False


async def test_saved_message_with_undetermined_run_is_not_reported_as_sent(environment) -> None:
    """A stored message whose run is unknown must not read as delivered-and-running."""
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=FakeProvider(message_started=None),
            policy=PolicyEngine(),
        )
    )

    result = await server.call_tool(
        "send_task_message",
        {"environment_id": "local", "task_id": "t1", "message": "hi"},
    )
    body = payload(result)

    assert body["saved"] is True
    assert body["started"] is None
    assert body["sent"] is False
    assert "unknown" in body["detail"]


async def test_saved_message_with_started_run_is_reported_as_sent(environment) -> None:
    """The honest states must not turn a real success into a maybe."""
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=FakeProvider(message_started=True),
            policy=PolicyEngine(),
        )
    )

    result = await server.call_tool(
        "send_task_message",
        {"environment_id": "local", "task_id": "t1", "message": "hi"},
    )
    body = payload(result)

    assert body["saved"] is True
    assert body["started"] is True
    assert body["sent"] is True


async def test_ambiguous_write_is_audited_as_unknown_through_the_server(environment) -> None:
    """The audit trail reached via a real tool call must not claim failure."""
    from openhands_connect.domain.models import AuditOutcome
    from openhands_connect.tests.conftest import RecordingAudit

    audit = RecordingAudit()
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=FakeProvider(
                fail_with=ProviderAmbiguousWrite("may or may not have been applied")
            ),
            policy=PolicyEngine(),
            audit=audit,
        )
    )

    result = await server.call_tool(
        "send_task_message",
        {"environment_id": "local", "task_id": "t1", "message": "hi"},
    )
    body = payload(result)

    assert body["error"] == "provider_ambiguous"
    assert audit.events[-1].outcome is AuditOutcome.UNKNOWN
    assert audit.events[-1].task_id == "t1"


async def test_stop_task_reports_that_it_was_not_cancelled(server) -> None:
    result = await server.call_tool("stop_task", {"environment_id": "local", "task_id": "t1"})
    body = payload(result)

    assert body["cancelled"] is False
    assert body["state"] == "paused"
    assert "interrupted" in body["detail"]


async def test_get_task_result_when_unavailable_says_so(server, fake_provider) -> None:
    fake_provider.result_available = False

    result = await server.call_tool("get_task_result", {"environment_id": "local", "task_id": "t1"})
    body = payload(result)

    assert body["available"] is False
    assert body["summary"] is None


async def test_send_task_message_reports_delivery(server, fake_provider) -> None:
    result = await server.call_tool(
        "send_task_message",
        {"environment_id": "local", "task_id": "t1", "message": "also update the docs"},
    )
    body = payload(result)

    assert body["sent"] is True
    assert body["saved"] is True
    assert body["started"] is True
    assert fake_provider.sent_messages == [("t1", "also update the docs")]


async def test_send_task_message_reports_a_stored_but_unstarted_message(
    server, fake_provider
) -> None:
    """A message whose run was refused must not read as delivered.

    ``sent`` means "the run will act on it"; a stored message whose run was
    refused by the server (429 run capacity) is saved but not sent.
    """
    fake_provider.message_started = False

    result = await server.call_tool(
        "send_task_message",
        {"environment_id": "local", "task_id": "t1", "message": "hi"},
    )
    body = payload(result)

    assert body["saved"] is True
    assert body["started"] is False
    assert body["sent"] is False
    assert "capacity" in body["detail"] or "did not start" in body["detail"]


async def test_logs_are_returned_with_truncation_flags(server, fake_provider) -> None:
    from openhands_connect.domain.models import TaskLogEntry

    fake_provider.log_entries = tuple(
        TaskLogEntry(kind="MessageEvent", text=f"entry {index}", source="agent")
        for index in range(10)
    )

    result = await server.call_tool(
        "get_task_logs", {"environment_id": "local", "task_id": "t1", "limit": 4}
    )
    body = payload(result)

    assert len(body["entries"]) == 4
    assert body["truncated"] is True
    assert body["dropped_entries"] == 6


async def test_log_limit_schema_matches_the_upstream_maximum(server) -> None:
    """The advertised maximum must be one the server accepts.

    Upstream ``events/search`` caps ``limit`` at 100 (422 above it), so a schema
    offering 200 let a caller request a value that could only fail.
    """
    by_name = {tool.name: tool for tool in await server.list_tools()}
    limit_schema = by_name["get_task_logs"].input_schema["properties"]["limit"]

    assert limit_schema["maximum"] == 100
    assert "1-100" in limit_schema["description"]
    # The tool layer keeps its own copy to stay free of upstream details; the
    # two bounds must not drift.
    from openhands_connect.adapters.openhands import MAX_LOG_ENTRIES as ADAPTER_MAX

    assert limit_schema["maximum"] == ADAPTER_MAX


async def test_unreachable_server_is_reported_as_a_provider_failure() -> None:
    """A refused connection must not surface as ``internal_error``.

    ``error_payload`` only recognizes ``ProviderError``; a raw ``httpx`` error
    escaping the adapter fell through to ``internal_error``, which told a caller
    nothing about the cause and offered no retry hint.
    """
    import httpx

    environment = make_environment()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=OpenHandsAdapter(lambda _env: None, transport=httpx.MockTransport(handler)),
            policy=PolicyEngine(),
        )
    )

    body = payload(
        await server.call_tool("create_task", {"environment_id": "local", "prompt": "x"})
    )

    assert body["error"] == "provider_unavailable"
    assert body["retryable"] is True
    assert "internal_error" not in json.dumps(body)


async def test_no_credential_appears_in_any_tool_output(environment, audit) -> None:
    """End-to-end sweep: the token must not appear in any tool response."""
    secret = "sup3r-s3cret-session-key"
    responses: list[str] = []

    def handler(request) -> Any:
        import httpx

        if request.url.path == "/server_info":
            return httpx.Response(200, json={"version": "1.60.1"})
        if request.url.path == "/api/conversations":
            return httpx.Response(201, json={"id": "t1", "execution_status": "running"})
        if "events/search" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "e1",
                            "kind": "MessageEvent",
                            "source": "agent",
                            "llm_message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": f"authenticated with {secret}"}
                                ],
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/agent_final_response"):
            return httpx.Response(200, json={"response": f"done using {secret}"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"success": True})
        if request.url.path.endswith("/interrupt"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"id": "t1", "execution_status": "finished"})

    adapter = OpenHandsAdapter(
        lambda _env: secret,
        transport=__import__("httpx").MockTransport(handler),
    )
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=adapter,
            policy=PolicyEngine(),
            audit=audit,
        )
    )

    calls = [
        ("list_environments", {}),
        ("check_environment", {"environment_id": "local"}),
        ("create_task", {"environment_id": "local", "prompt": "go"}),
        ("get_task", {"environment_id": "local", "task_id": "t1"}),
        ("get_task_logs", {"environment_id": "local", "task_id": "t1"}),
        ("send_task_message", {"environment_id": "local", "task_id": "t1", "message": "hi"}),
        ("stop_task", {"environment_id": "local", "task_id": "t1"}),
        ("get_task_result", {"environment_id": "local", "task_id": "t1"}),
    ]
    for name, arguments in calls:
        responses.append(json.dumps(payload(await server.call_tool(name, arguments))))

    combined = "\n".join(responses)
    assert secret not in combined
    assert "REDACTED" in combined


async def test_execution_operations_are_required_for_execution_tools() -> None:
    """A read-only environment refuses every execution tool, not just create."""
    environment = make_environment("readonly", allowed=frozenset(READ_OPERATIONS))
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=FakeProvider(),
            policy=PolicyEngine(),
        )
    )

    for name, arguments in [
        ("create_task", {"prompt": "x"}),
        ("send_task_message", {"task_id": "t1", "message": "y"}),
        ("stop_task", {"task_id": "t1"}),
    ]:
        body = payload(await server.call_tool(name, {"environment_id": "readonly", **arguments}))
        assert body["error"] == "policy_denied", name


def test_execution_operations_are_opt_in_by_default() -> None:
    """Config defaults: nothing in EXECUTION_OPERATIONS is allowed implicitly."""
    environment = make_environment()
    # Even a fully-permitted environment must have opted in explicitly.
    assert EXECUTION_OPERATIONS.issubset(environment.allowed_operations)


async def test_mcp_boundary_redacts_credentials_in_every_text_field() -> None:
    """Defense in depth: a leaked secret in upstream text must not reach a caller.

    The adapter redacts first, but a value it misses (or a future provider that
    does not redact at all) must still be scrubbed here.
    """
    secret = "sk-live-9f8e7d6c5b4a39281706"
    environment = make_environment()
    provider = FakeProvider(
        health_detail=f"ok, token={secret}",
        task_title=f"title {secret}",
        task_detail=f"detail {secret}",
        result_summary=f"finished with {secret}",
        log_entries=(
            TaskLogEntry(
                timestamp="2026-01-01T00:00:00Z",
                kind="log",
                source="agent",
                text=f"using key {secret}",
            ),
        ),
    )
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=provider,
            policy=PolicyEngine(),
        )
    )

    bodies = [
        payload(await server.call_tool("check_environment", {"environment_id": "local"})),
        payload(await server.call_tool("create_task", {"environment_id": "local", "prompt": "x"})),
        payload(await server.call_tool("get_task", {"environment_id": "local", "task_id": "t1"})),
        payload(
            await server.call_tool("get_task_logs", {"environment_id": "local", "task_id": "t1"})
        ),
        payload(
            await server.call_tool("get_task_result", {"environment_id": "local", "task_id": "t1"})
        ),
    ]

    for body in bodies:
        assert secret not in json.dumps(body), body


async def test_check_environment_reports_the_credential_state(
    environment: Environment,
) -> None:
    """The tool must expose the credential state, not just reachable.

    A caller has to be able to tell "server down" from "my token is wrong"
    without parsing prose, so the state is a structured field.
    """
    provider = FakeProvider(reachable=False, credential_state=CredentialState.REJECTED)
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=provider,
            policy=PolicyEngine(),
        )
    )

    body = payload(await server.call_tool("check_environment", {"environment_id": "local"}))

    assert body["reachable"] is False
    assert body["credential_state"] == "rejected"


# -- log pagination through the tool surface --------------------------------


async def test_logs_default_to_newest_first_and_advertise_continuation(
    server, fake_provider
) -> None:
    """An unpaged read must surface recent activity and say how to continue."""
    fake_provider.log_entries = tuple(
        TaskLogEntry(kind="MessageEvent", text=f"entry {index}", source="agent")
        for index in range(10)
    )

    body = payload(
        await server.call_tool(
            "get_task_logs", {"environment_id": "local", "task_id": "t1", "limit": 4}
        )
    )

    assert body["sort_order"] == "newest_first"
    assert body["entries"][0]["text"] == "entry 9"
    assert body["has_more"] is True
    assert body["next_cursor"] is not None
    assert fake_provider.log_calls[-1]["sort_order"] is None


async def test_log_cursor_is_passed_through_to_the_provider(server, fake_provider) -> None:
    """A cursor the tool returns must reach the provider unchanged."""
    fake_provider.log_entries = tuple(
        TaskLogEntry(kind="MessageEvent", text=f"entry {index}", source="agent")
        for index in range(10)
    )

    first = payload(
        await server.call_tool(
            "get_task_logs", {"environment_id": "local", "task_id": "t1", "limit": 4}
        )
    )
    await server.call_tool(
        "get_task_logs",
        {"environment_id": "local", "task_id": "t1", "limit": 4, "cursor": first["next_cursor"]},
    )

    assert fake_provider.log_calls[-1]["cursor"] == first["next_cursor"]


async def test_log_sort_order_is_advertised_in_the_schema(server) -> None:
    by_name = {tool.name: tool for tool in await server.list_tools()}
    props = by_name["get_task_logs"].input_schema["properties"]

    assert "cursor" in props
    assert "sort_order" in props
    # Nullable and optional so a continuation can omit it and inherit the
    # cursor's direction instead of silently flipping it.
    assert {"type": "null"} in props["sort_order"]["anyOf"]
    assert "cursor" in props["sort_order"]["description"]
