"""Policy decisions and explicit environment routing."""

from __future__ import annotations

import pytest

from openhands_connect.domain.errors import UnknownEnvironmentError
from openhands_connect.domain.models import (
    EXECUTION_OPERATIONS,
    READ_OPERATIONS,
    Environment,
    Operation,
)
from openhands_connect.domain.registry import EnvironmentRegistry
from openhands_connect.policy import PolicyEngine
from openhands_connect.tests.conftest import make_environment

READ_OP_CASES: list[Operation] = sorted(READ_OPERATIONS, key=lambda op: op.value)
EXEC_OP_CASES: list[Operation] = sorted(EXECUTION_OPERATIONS, key=lambda op: op.value)


@pytest.mark.parametrize("operation", READ_OP_CASES)
def test_read_operations_allowed_when_configured(
    environment: Environment, operation: Operation
) -> None:
    decision = PolicyEngine().evaluate(environment, operation)
    assert decision.allowed is True
    assert decision.environment_id == environment.id


@pytest.mark.parametrize("operation", EXEC_OP_CASES)
def test_execution_operations_allowed_when_configured(
    environment: Environment, operation: Operation
) -> None:
    assert PolicyEngine().evaluate(environment, operation).allowed is True


@pytest.mark.parametrize("operation", EXEC_OP_CASES)
def test_execution_denied_by_default(
    read_only_environment: Environment, operation: Operation
) -> None:
    """An environment that did not opt in must not permit execution."""
    decision = PolicyEngine().evaluate(read_only_environment, operation)
    assert decision.allowed is False
    assert "not permitted" in decision.reason


def test_disabled_environment_permits_nothing(disabled_environment: Environment) -> None:
    for operation in (Operation.CHECK_ENVIRONMENT, Operation.CREATE_TASK):
        decision = PolicyEngine().evaluate(disabled_environment, operation)
        assert decision.allowed is False
        assert "disabled" in decision.reason


def test_denied_reason_names_the_allowed_set(read_only_environment: Environment) -> None:
    decision = PolicyEngine().evaluate(read_only_environment, Operation.CREATE_TASK)
    assert "allowed:" in decision.reason
    assert "check_environment" in decision.reason


# -- registry ---------------------------------------------------------------


def test_registry_returns_environment_by_id(environment: Environment) -> None:
    registry = EnvironmentRegistry((environment,))
    assert registry.get("local") is environment
    assert len(registry) == 1
    assert "local" in registry


def test_registry_has_no_default() -> None:
    """Constructing a registry with no environments gives no fallback target."""
    registry = EnvironmentRegistry(())
    assert registry.list() == ()
    with pytest.raises(UnknownEnvironmentError, match="none configured"):
        registry.get("anything")


def test_unknown_environment_lists_configured_ids(environment: Environment) -> None:
    registry = EnvironmentRegistry((environment,))
    with pytest.raises(UnknownEnvironmentError, match="local"):
        registry.get("cloud")


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate environment ids"):
        EnvironmentRegistry((make_environment("dup"), make_environment("dup")))


def test_registry_listing_is_sorted_and_stable() -> None:
    registry = EnvironmentRegistry(
        (make_environment("zeta"), make_environment("alpha"), make_environment("mid"))
    )
    assert [env.id for env in registry.list()] == ["alpha", "mid", "zeta"]
