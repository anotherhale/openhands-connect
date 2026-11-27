"""Server wiring: a missing credential must not disable fail-closed behaviour.

These tests build the real service from a real config file, because the P1 bug
being guarded against lived in that wiring: the credential provider swallowed
``ConfigurationError`` and returned ``None``, so an environment whose key was
absent was silently treated as unauthenticated.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from openhands_connect.domain.errors import ConfigurationError
from openhands_connect.mcp_server.__main__ import AUDIT_LOG_ENV_VAR, build_service

CONFIG = """\
schema_version: 1
environments:
  - id: local
    name: Local
    provider: openhands
    base_url: http://127.0.0.1:18000
    credential_env_var: OH_TEST_MISSING_KEY
    allowed_operations:
      - check_environment
      - create_task
      - get_task
      - get_task_logs
      - get_task_result
      - send_task_message
      - stop_task
"""


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    path = tmp_path / "environments.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("OH_CONNECT_CONFIG", str(path))
    monkeypatch.delenv("OH_TEST_MISSING_KEY", raising=False)
    # Keep audit output out of captured stderr noise; the sink is irrelevant here.
    monkeypatch.delenv(AUDIT_LOG_ENV_VAR, raising=False)
    yield


def test_server_builds_when_a_credential_is_absent(configured: None) -> None:
    """Startup and listing must keep working with an incomplete setup."""
    service = build_service()

    environments = service.list_environments()
    assert [environment.id for environment in environments] == ["local"]


async def test_operation_on_absent_credential_fails_closed(configured: None) -> None:
    """The provider must raise, not return None and send an anonymous request."""
    service = build_service()

    with pytest.raises(ConfigurationError, match="OH_TEST_MISSING_KEY"):
        await service.check_environment("local")
