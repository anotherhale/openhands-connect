"""Configuration loading.

Config files hold *references* to credentials (environment variable names),
never credential values. A token can only arrive at runtime through the
process environment, which keeps secrets out of committed files and out of
any config object that might be logged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openhands_connect.domain.errors import ConfigurationError
from openhands_connect.domain.models import (
    EXECUTION_OPERATIONS,
    Environment,
    Operation,
    ProviderKind,
)

DEFAULT_CONFIG_PATH = Path("config/environments.yaml")
CONFIG_PATH_ENV_VAR = "OH_CONNECT_CONFIG"


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EnvironmentConfig(_ConfigModel):
    """One entry in the config file.

    ``credential_env_var`` names an environment variable; the value is read at
    call time and never stored on this model.
    """

    id: str
    name: str
    provider: ProviderKind = ProviderKind.OPENHANDS
    base_url: str
    enabled: bool = True
    credential_env_var: str | None = None
    allowed_operations: Annotated[list[Operation], Field(min_length=0)] = Field(
        default_factory=list
    )
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    default_agent_profile_id: str | None = None

    @field_validator("credential_env_var")
    @classmethod
    def _validate_env_var_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # An empty name would silently mean "no credential"; reject it so a
        # typo cannot turn an authenticated environment into an anonymous one.
        if not value:
            raise ValueError(
                "credential_env_var must not be empty; omit it for an "
                "environment that needs no credential"
            )
        if not value.replace("_", "").isalnum() or value[0].isdigit():
            raise ValueError(
                "credential_env_var must be an environment variable name, not a credential value"
            )
        return value

    def to_environment(self) -> Environment:
        allowed = set(self.allowed_operations)
        try:
            return Environment(
                id=self.id,
                name=self.name,
                provider=self.provider,
                base_url=self.base_url,
                enabled=self.enabled,
                allowed_operations=frozenset(allowed),
                credential_env_var=self.credential_env_var,
                request_timeout_seconds=self.request_timeout_seconds,
                max_attempts=self.max_attempts,
                default_agent_profile_id=self.default_agent_profile_id,
            )
        except ValidationError as exc:
            # Domain-level rules (id pattern, URL shape) live on Environment, so
            # their failures must surface as a config error naming the entry.
            raise ConfigurationError(
                f"environment {self.id!r} is invalid:\n{_format_validation_error(exc)}"
            ) from exc


class ConnectConfig(_ConfigModel):
    schema_version: int = 1
    environments: Annotated[list[EnvironmentConfig], Field(min_length=1)]

    @field_validator("schema_version")
    @classmethod
    def _only_v1(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported config schema_version {value}; expected 1")
        return value


class LoadedConfig:
    """Configuration plus the credentials resolved for this process."""

    def __init__(self, environments: tuple[Environment, ...], credentials: dict[str, str]) -> None:
        self.environments = environments
        self._credentials = credentials

    def credential_for(self, environment: Environment) -> str | None:
        """Return the token for an environment, or ``None`` when it needs none."""
        if environment.credential_env_var is None:
            return None
        value = self._credentials.get(environment.id)
        if not value:
            raise ConfigurationError(
                f"environment {environment.id!r} requires credential env var "
                f"{environment.credential_env_var}, which is not set"
            )
        return value


def load_config(
    path: str | Path | None = None, *, env: dict[str, str] | None = None
) -> LoadedConfig:
    """Load and validate configuration, resolving credential references."""
    environ = os.environ if env is None else env
    resolved_path = Path(path or environ.get(CONFIG_PATH_ENV_VAR) or DEFAULT_CONFIG_PATH)

    if not resolved_path.exists():
        raise ConfigurationError(
            f"config file not found: {resolved_path}. "
            f"Copy examples/environments.example.yaml and set {CONFIG_PATH_ENV_VAR}."
        )

    try:
        raw: Any = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"config file {resolved_path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise ConfigurationError(f"config file {resolved_path} is empty")
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"config file {resolved_path} must contain a mapping at the top level"
        )

    try:
        config = ConnectConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(
            f"config file {resolved_path} is invalid:\n{_format_validation_error(exc)}"
        ) from exc

    environments = tuple(entry.to_environment() for entry in config.environments)
    credentials = {
        entry.id: environ[entry.credential_env_var]
        for entry in config.environments
        if entry.credential_env_var and entry.credential_env_var in environ
    }
    return LoadedConfig(environments, credentials)


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


__all__ = [
    "CONFIG_PATH_ENV_VAR",
    "DEFAULT_CONFIG_PATH",
    "EXECUTION_OPERATIONS",
    "ConnectConfig",
    "EnvironmentConfig",
    "LoadedConfig",
    "load_config",
]
