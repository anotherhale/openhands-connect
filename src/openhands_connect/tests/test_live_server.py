"""Integration tests against a REAL OpenHands Agent Server.

Skipped unless ``OH_CONNECT_INTEGRATION_BASE_URL`` is set, so the default test
run never needs a network or a running server. When it is set, these tests talk
to the actual Agent Server over HTTP through the real adapter — no transport
substitution — and assert on real conversation IDs and real lifecycle states.

Run against a local server with:

    OH_CONNECT_INTEGRATION_BASE_URL=http://127.0.0.1:18000 \
        uv run pytest -m integration
"""

from __future__ import annotations

import os

import pytest

from openhands_connect.adapters.openhands import OpenHandsAdapter
from openhands_connect.domain.models import (
    ProviderError,
    TaskRequest,
    TaskState,
)
from openhands_connect.tests.conftest import make_environment

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("OH_CONNECT_INTEGRATION_BASE_URL")
API_KEY = os.environ.get("OH_CONNECT_INTEGRATION_API_KEY")
requires_server = pytest.mark.skipif(
    not BASE_URL, reason="OH_CONNECT_INTEGRATION_BASE_URL is not set"
)


@pytest.fixture
def live_environment():
    return make_environment(
        "live",
        base_url=BASE_URL,
        request_timeout_seconds=120,
        # The server rejects a conversation with no agent source; this profile
        # is the one active on the local dev server.
        default_agent_profile_id=os.environ.get(
            "OH_CONNECT_INTEGRATION_AGENT_PROFILE", "autonomous"
        ),
        credential_env_var="OH_CONNECT_INTEGRATION_API_KEY" if API_KEY else None,
    )


@pytest.fixture
def live_adapter() -> OpenHandsAdapter:
    return OpenHandsAdapter(lambda _env: API_KEY)


@requires_server
async def test_live_server_is_reachable(live_adapter, live_environment) -> None:
    health = await live_adapter.health_check(live_environment)

    assert health.reachable is True, health.detail
    assert health.server_version, "server reported no version"


@requires_server
async def test_live_task_lifecycle(live_adapter, live_environment) -> None:
    """Create a real conversation, observe it, read its events, then pause it."""
    task = await live_adapter.create_task(
        live_environment,
        TaskRequest(
            prompt="Reply with exactly the word PONG and nothing else.",
            max_iterations=3,
        ),
    )

    assert task.id
    assert task.environment_id == "live"
    # A freshly created conversation is dispatched but not yet finished.
    assert task.state in {
        TaskState.DISPATCHED,
        TaskState.RUNNING,
        TaskState.COMPLETED,
        TaskState.PAUSED,
    }

    fetched = await live_adapter.get_task(live_environment, task.id)
    assert fetched.id == task.id
    assert fetched.provider_state is not None

    logs = await live_adapter.get_task_logs(live_environment, task.id, limit=20)
    assert logs.task_id == task.id

    stopped = await live_adapter.stop_task(live_environment, task.id)
    assert stopped.task_id == task.id
    # The honest contract: interrupting pauses, it does not cancel.
    assert stopped.cancelled is False


@requires_server
async def test_live_unknown_task_raises_not_found(live_adapter, live_environment) -> None:
    with pytest.raises(ProviderError):
        await live_adapter.get_task(live_environment, "00000000-0000-4000-8000-000000000000")
