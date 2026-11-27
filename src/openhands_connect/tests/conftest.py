"""Shared fixtures: environments, config files, and a wired service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openhands_connect.audit import AuditSink
from openhands_connect.domain.models import (
    EXECUTION_OPERATIONS,
    READ_OPERATIONS,
    AuditEvent,
    Environment,
    Operation,
    ProviderKind,
)
from openhands_connect.domain.registry import EnvironmentRegistry
from openhands_connect.policy import PolicyEngine
from openhands_connect.service import ConnectService
from openhands_connect.tests.fakes import FakeProvider


class RecordingAudit(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def make_environment(
    environment_id: str = "local",
    *,
    enabled: bool = True,
    allowed: frozenset[Operation] | None = None,
    **overrides: Any,
) -> Environment:
    payload: dict[str, Any] = {
        "id": environment_id,
        "name": f"{environment_id} environment",
        "provider": ProviderKind.OPENHANDS,
        "base_url": "http://127.0.0.1:18000",
        "enabled": enabled,
        "allowed_operations": allowed
        if allowed is not None
        else frozenset(READ_OPERATIONS | EXECUTION_OPERATIONS),
        "credential_env_var": "OH_TEST_API_KEY",
        "default_agent_profile_id": "11111111-2222-3333-4444-555555555555",
    }
    payload.update(overrides)
    return Environment(**payload)


@pytest.fixture
def environment() -> Environment:
    return make_environment()


@pytest.fixture
def read_only_environment() -> Environment:
    return make_environment("readonly", allowed=frozenset(READ_OPERATIONS))


@pytest.fixture
def disabled_environment() -> Environment:
    return make_environment("disabled", enabled=False)


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def audit() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture
def service(
    environment: Environment, fake_provider: FakeProvider, audit: RecordingAudit
) -> ConnectService:
    return ConnectService(
        registry=EnvironmentRegistry((environment,)),
        provider=fake_provider,
        policy=PolicyEngine(),
        audit=audit,
    )


@pytest.fixture
def config_file(tmp_path: Path) -> Any:
    """Write a config file and return a factory for variants."""

    def _write(body: str, name: str = "environments.yaml") -> Path:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path

    return _write


VALID_CONFIG = """\
schema_version: 1
environments:
  - id: local
    name: Little Mac
    provider: openhands
    base_url: http://127.0.0.1:18000
    enabled: true
    credential_env_var: OH_LOCAL_KEY
    allowed_operations:
      - check_environment
      - create_task
      - get_task
      - get_task_logs
      - get_task_result
      - send_task_message
      - stop_task
    request_timeout_seconds: 15
    default_agent_profile_id: 11111111-2222-3333-4444-555555555555
"""
