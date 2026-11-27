"""Environment registry: named, explicit targets with no implicit fallback."""

from __future__ import annotations

from collections import Counter

from openhands_connect.domain.errors import UnknownEnvironmentError
from openhands_connect.domain.models import Environment, EnvironmentId


class EnvironmentRegistry:
    """Lookup over explicitly configured environments.

    There is intentionally no ``default`` and no ``fallback``: a caller must
    name the environment for every call, and an unknown name is an error rather
    than a route to "whatever is configured".
    """

    def __init__(self, environments: tuple[Environment, ...]) -> None:
        duplicates = sorted(
            env_id for env_id, count in Counter(env.id for env in environments).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate environment ids: {duplicates}")
        self._by_id: dict[str, Environment] = {env.id: env for env in environments}

    def list(self) -> tuple[Environment, ...]:
        return tuple(self._by_id[key] for key in sorted(self._by_id))

    def get(self, environment_id: EnvironmentId) -> Environment:
        try:
            return self._by_id[environment_id]
        except KeyError:
            known = ", ".join(sorted(self._by_id)) or "<none configured>"
            raise UnknownEnvironmentError(
                f"unknown environment {environment_id!r}; configured: {known}"
            ) from None

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, environment_id: object) -> bool:
        return environment_id in self._by_id
