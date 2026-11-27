"""Entry point for the OpenHands Connect MCP server.

Configuration comes from ``OH_CONNECT_CONFIG`` (or ``config/environments.yaml``).
Audit events go to stderr by default, or to ``OH_CONNECT_AUDIT_LOG`` when set.
Credentials are read from the environment variable each environment names; no
credential is ever accepted on the command line or from the config file.
"""

from __future__ import annotations

import logging
import os
import sys

from openhands_connect.adapters.openhands import OpenHandsAdapter
from openhands_connect.audit import FileAuditSink, StreamAuditSink
from openhands_connect.config import LoadedConfig, load_config
from openhands_connect.domain.errors import ConfigurationError
from openhands_connect.domain.registry import EnvironmentRegistry
from openhands_connect.mcp_server.server import build_server
from openhands_connect.policy import PolicyEngine
from openhands_connect.redaction import Redactor
from openhands_connect.service import ConnectService

AUDIT_LOG_ENV_VAR = "OH_CONNECT_AUDIT_LOG"


def build_service() -> ConnectService:
    config = load_config()
    redactor = Redactor(_known_credentials(config))
    provider = OpenHandsAdapter(
        lambda environment: config.credential_for(environment), redactor=redactor
    )
    audit_path = os.environ.get(AUDIT_LOG_ENV_VAR)
    audit = FileAuditSink(audit_path) if audit_path else StreamAuditSink()
    return ConnectService(
        registry=EnvironmentRegistry(config.environments),
        provider=provider,
        policy=PolicyEngine(),
        audit=audit,
    )


def _known_credentials(config: LoadedConfig) -> tuple[str, ...]:
    """Credentials available now, for the redactor's literal set.

    Best effort: a credential that is configured but absent must not stop the
    server from starting, because listing environments has to keep working.
    The adapter resolves credentials strictly at call time and fails closed
    there, so an operation on an unconfigured environment cannot proceed.
    """
    values: list[str] = []
    for environment in config.environments:
        try:
            value = config.credential_for(environment)
        except ConfigurationError:
            continue
        if value:
            values.append(value)
    return tuple(values)


def main() -> None:
    try:
        service = build_service()
    except ConfigurationError as exc:
        logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
        logging.getLogger(__name__).error("openhands-connect: %s", exc)
        raise SystemExit(2) from exc
    build_server(service).run()


if __name__ == "__main__":
    main()
