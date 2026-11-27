"""Service-level behavior: policy gating, audit emission, routing."""

from __future__ import annotations

import pytest

from openhands_connect.domain.errors import PolicyDeniedError, UnknownEnvironmentError
from openhands_connect.domain.models import (
    READ_OPERATIONS,
    AuditOutcome,
    Operation,
    ProviderUnavailable,
    TaskRequest,
    TaskState,
)
from openhands_connect.domain.registry import EnvironmentRegistry
from openhands_connect.policy import PolicyEngine
from openhands_connect.service import ConnectService
from openhands_connect.tests.conftest import RecordingAudit, make_environment
from openhands_connect.tests.fakes import FailingProvider, FakeProvider


async def test_create_task_routes_to_named_environment(
    service: ConnectService, fake_provider: FakeProvider
) -> None:
    task = await service.create_task("local", TaskRequest(prompt="do work"))

    assert task.environment_id == "local"
    assert fake_provider.calls == [("create_task", "local")]
    assert fake_provider.created_requests[0].prompt == "do work"


async def test_denied_execution_never_reaches_provider(
    read_only_environment, fake_provider: FakeProvider, audit: RecordingAudit
) -> None:
    """A policy denial must short-circuit before any provider call."""
    service = ConnectService(
        registry=EnvironmentRegistry((read_only_environment,)),
        provider=fake_provider,
        policy=PolicyEngine(),
        audit=audit,
    )

    with pytest.raises(PolicyDeniedError):
        await service.create_task("readonly", TaskRequest(prompt="rm -rf /"))

    assert fake_provider.calls == []
    assert [event.outcome for event in audit.events] == [AuditOutcome.DENIED]
    assert audit.events[0].operation is Operation.CREATE_TASK


async def test_unknown_environment_is_rejected_before_provider(
    service: ConnectService, fake_provider: FakeProvider
) -> None:
    with pytest.raises(UnknownEnvironmentError):
        await service.create_task("nowhere", TaskRequest(prompt="x"))
    assert fake_provider.calls == []


async def test_successful_call_emits_succeeded_audit(
    service: ConnectService, audit: RecordingAudit
) -> None:
    await service.get_task("local", "task-1")

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.outcome is AuditOutcome.SUCCEEDED
    assert event.operation is Operation.GET_TASK
    assert event.task_id == "task-1"
    assert event.environment_id == "local"


async def test_provider_failure_emits_failed_audit_and_reraises(
    environment, audit: RecordingAudit
) -> None:
    provider = FailingProvider(fail_with=ProviderUnavailable("server exploded"))
    service = ConnectService(
        registry=EnvironmentRegistry((environment,)),
        provider=provider,
        policy=PolicyEngine(),
        audit=audit,
    )

    with pytest.raises(ProviderUnavailable):
        await service.get_task("local", "task-9")

    assert audit.events[0].outcome is AuditOutcome.FAILED
    assert audit.events[0].task_id == "task-9"


async def test_audit_event_has_no_payload_fields(
    service: ConnectService, audit: RecordingAudit
) -> None:
    """The audit record must be incapable of carrying prompt text."""
    await service.create_task("local", TaskRequest(prompt="SECRET-PROMPT-TEXT"))

    serialized = audit.events[0].model_dump_json()
    assert "SECRET-PROMPT-TEXT" not in serialized
    assert set(audit.events[0].model_dump()) == {
        "timestamp",
        "environment_id",
        "operation",
        "outcome",
        "task_id",
        "detail",
        "principal",
        "schema_version",
    }


async def test_list_environments_needs_no_policy_or_provider(
    service: ConnectService, fake_provider: FakeProvider
) -> None:
    environments = service.list_environments()
    assert [env.id for env in environments] == ["local"]
    assert fake_provider.calls == []


async def test_all_read_operations_work_on_read_only_environment(read_only_environment) -> None:
    provider = FakeProvider()
    service = ConnectService(
        registry=EnvironmentRegistry((read_only_environment,)),
        provider=provider,
        policy=PolicyEngine(),
    )

    await service.check_environment("readonly")
    await service.get_task("readonly", "t1")
    await service.get_task_logs("readonly", "t1")
    await service.get_task_result("readonly", "t1")

    assert {operation for operation, _ in provider.calls} == {
        "health_check",
        "get_task",
        "get_task_logs",
        "get_task_result",
    }


async def test_each_environment_only_permits_its_own_operations() -> None:
    """Two environments with different policy must not share permissions."""
    open_env = make_environment("open")
    locked = make_environment("locked", allowed=frozenset(READ_OPERATIONS))
    service = ConnectService(
        registry=EnvironmentRegistry((open_env, locked)),
        provider=FakeProvider(),
        policy=PolicyEngine(),
    )

    await service.create_task("open", TaskRequest(prompt="ok"))
    with pytest.raises(PolicyDeniedError):
        await service.create_task("locked", TaskRequest(prompt="nope"))


async def test_task_state_passes_through_service(service: ConnectService) -> None:
    task = await service.get_task("local", "task-1")
    assert task.state is TaskState.RUNNING
