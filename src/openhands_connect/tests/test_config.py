"""Configuration parsing, validation and credential-reference handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from openhands_connect.config import load_config
from openhands_connect.domain.errors import ConfigurationError
from openhands_connect.domain.models import EXECUTION_OPERATIONS, Operation
from openhands_connect.tests.conftest import VALID_CONFIG


def test_loads_valid_config(config_file) -> None:
    path = config_file(VALID_CONFIG)
    loaded = load_config(path, env={"OH_LOCAL_KEY": "token-abc"})

    assert len(loaded.environments) == 1
    environment = loaded.environments[0]
    assert environment.id == "local"
    assert environment.base_url == "http://127.0.0.1:18000"
    assert environment.request_timeout_seconds == 15
    assert environment.default_agent_profile_id == "11111111-2222-3333-4444-555555555555"
    assert Operation.CREATE_TASK in environment.allowed_operations
    assert loaded.credential_for(environment) == "token-abc"


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="config file not found"):
        load_config(tmp_path / "absent.yaml", env={})


def test_empty_config_file_is_reported(config_file) -> None:
    with pytest.raises(ConfigurationError, match="is empty"):
        load_config(config_file(""), env={})


def test_non_mapping_config_is_reported(config_file) -> None:
    with pytest.raises(ConfigurationError, match="must contain a mapping"):
        load_config(config_file("- just\n- a list\n"), env={})


def test_invalid_yaml_is_reported(config_file) -> None:
    with pytest.raises(ConfigurationError, match="not valid YAML"):
        load_config(config_file("environments: [unclosed\n"), env={})


def test_unknown_top_level_key_is_rejected(config_file) -> None:
    with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
        load_config(config_file(VALID_CONFIG + "unexpected: true\n"), env={})


def test_empty_environment_list_is_rejected(config_file) -> None:
    with pytest.raises(ConfigurationError, match="environments"):
        load_config(config_file("schema_version: 1\nenvironments: []\n"), env={})


def test_unsupported_schema_version_is_rejected(config_file) -> None:
    body = VALID_CONFIG.replace("schema_version: 1", "schema_version: 2")
    with pytest.raises(ConfigurationError, match="schema_version"):
        load_config(config_file(body), env={})


def test_credential_value_in_config_is_rejected(config_file) -> None:
    """A token pasted where an env var name belongs must not be accepted."""
    body = VALID_CONFIG.replace("OH_LOCAL_KEY", "sk-live-abcdef123456")
    with pytest.raises(ConfigurationError, match="environment variable name"):
        load_config(config_file(body), env={})


def test_missing_credential_is_an_error_only_when_used(config_file) -> None:
    path = config_file(VALID_CONFIG)
    loaded = load_config(path, env={})

    # Loading and listing work without the secret present...
    assert loaded.environments[0].id == "local"
    # ...but resolving the credential reports precisely what is missing.
    with pytest.raises(ConfigurationError, match="OH_LOCAL_KEY"):
        loaded.credential_for(loaded.environments[0])


def test_environment_without_credential_reference_needs_none(config_file) -> None:
    body = VALID_CONFIG.replace("    credential_env_var: OH_LOCAL_KEY\n", "")
    loaded = load_config(config_file(body), env={})
    assert loaded.credential_for(loaded.environments[0]) is None


def test_empty_credential_env_var_is_rejected(config_file) -> None:
    """An empty name would silently mean 'no credential'; a typo must not do that."""
    body = VALID_CONFIG.replace("credential_env_var: OH_LOCAL_KEY", 'credential_env_var: ""')
    with pytest.raises(ConfigurationError, match="must not be empty"):
        load_config(config_file(body), env={})


def test_invalid_environment_id_is_rejected(config_file) -> None:
    body = VALID_CONFIG.replace("  - id: local", "  - id: Not Valid!")
    with pytest.raises(ConfigurationError, match="id"):
        load_config(config_file(body), env={})


def test_invalid_operation_is_rejected(config_file) -> None:
    body = VALID_CONFIG.replace("      - get_task\n", "      - deploy_everything\n")
    with pytest.raises(ConfigurationError, match="allowed_operations"):
        load_config(config_file(body), env={})


def test_config_path_comes_from_env_var(config_file) -> None:
    path = config_file(VALID_CONFIG, name="custom.yaml")
    loaded = load_config(env={"OH_CONNECT_CONFIG": str(path)})
    assert loaded.environments[0].id == "local"


def test_execution_operations_are_opt_in(config_file) -> None:
    """An environment listing only read operations permits no execution."""
    body = VALID_CONFIG.replace(
        "      - create_task\n"
        "      - get_task\n"
        "      - get_task_logs\n"
        "      - get_task_result\n"
        "      - send_task_message\n"
        "      - stop_task\n",
        "      - get_task\n      - get_task_logs\n      - get_task_result\n",
    )
    loaded = load_config(config_file(body), env={})
    environment = loaded.environments[0]
    assert environment.allowed_operations.isdisjoint(EXECUTION_OPERATIONS)
    assert Operation.CREATE_TASK not in environment.allowed_operations
