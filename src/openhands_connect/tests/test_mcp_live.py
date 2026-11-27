"""End-to-end integration through the MCP tool surface against a real server.

These exercise the exact code path a client uses: ``call_tool`` -> service ->
policy -> audit -> adapter -> HTTP -> real Agent Server. Skipped unless
``OH_CONNECT_INTEGRATION_BASE_URL`` is set.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from openhands_connect.adapters.openhands import OpenHandsAdapter
from openhands_connect.domain.models import READ_OPERATIONS
from openhands_connect.domain.registry import EnvironmentRegistry
from openhands_connect.mcp_server.server import build_server
from openhands_connect.policy import PolicyEngine
from openhands_connect.service import ConnectService
from openhands_connect.tests.conftest import RecordingAudit, make_environment

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("OH_CONNECT_INTEGRATION_BASE_URL")
API_KEY = os.environ.get("OH_CONNECT_INTEGRATION_API_KEY")
AGENT_PROFILE = os.environ.get("OH_CONNECT_INTEGRATION_AGENT_PROFILE", "autonomous")

requires_server = pytest.mark.skipif(
    not BASE_URL, reason="OH_CONNECT_INTEGRATION_BASE_URL is not set"
)


def payload(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


@requires_server
async def test_full_lifecycle_through_mcp_tools() -> None:
    environment = make_environment(
        "live",
        base_url=BASE_URL,
        request_timeout_seconds=120,
        default_agent_profile_id=AGENT_PROFILE,
        credential_env_var="OH_CONNECT_INTEGRATION_API_KEY" if API_KEY else None,
    )
    audit = RecordingAudit()
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=OpenHandsAdapter(lambda _env: API_KEY),
            policy=PolicyEngine(),
            audit=audit,
        )
    )

    listing = payload(await server.call_tool("list_environments", {}))
    assert listing["count"] == 1
    assert listing["environments"][0]["id"] == "live"

    health = payload(await server.call_tool("check_environment", {"environment_id": "live"}))
    assert health["reachable"] is True, health

    created = payload(
        await server.call_tool(
            "create_task",
            {"environment_id": "live", "prompt": "Reply with exactly: PONG", "max_iterations": 3},
        )
    )
    assert "error" not in created, created
    task_id = created["task_id"]
    assert task_id

    status = payload(
        await server.call_tool("get_task", {"environment_id": "live", "task_id": task_id})
    )
    assert status["task_id"] == task_id
    assert status["state"] in {"dispatched", "running", "completed", "paused", "failed"}

    logs = payload(
        await server.call_tool(
            "get_task_logs", {"environment_id": "live", "task_id": task_id, "limit": 10}
        )
    )
    assert logs["task_id"] == task_id
    assert isinstance(logs["entries"], list)

    result = payload(
        await server.call_tool("get_task_result", {"environment_id": "live", "task_id": task_id})
    )
    assert result["task_id"] == task_id
    # A task that has not completed must report available=false, not fake output.
    if result["state"] != "completed":
        assert result["available"] is False

    stopped = payload(
        await server.call_tool("stop_task", {"environment_id": "live", "task_id": task_id})
    )
    assert stopped["cancelled"] is False

    assert any(event.environment_id == "live" for event in audit.events)


@requires_server
async def test_policy_still_blocks_execution_against_a_real_server() -> None:
    """Policy is enforced before the network, even with a live server configured."""
    environment = make_environment(
        "readonly-live",
        base_url=BASE_URL,
        allowed=frozenset(READ_OPERATIONS),
        credential_env_var="OH_CONNECT_INTEGRATION_API_KEY" if API_KEY else None,
    )
    server = build_server(
        ConnectService(
            registry=EnvironmentRegistry((environment,)),
            provider=OpenHandsAdapter(lambda _env: API_KEY),
            policy=PolicyEngine(),
        )
    )

    body = payload(
        await server.call_tool("create_task", {"environment_id": "readonly-live", "prompt": "x"})
    )
    assert body["error"] == "policy_denied"
