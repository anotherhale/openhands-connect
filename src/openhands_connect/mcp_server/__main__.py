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
from openhands_connect.domain.models import Environment
from openhands_connect.domain.registry import EnvironmentRegistry
from openhands_connect.mcp_server.server import build_server
from openhands_connect.policy import PolicyEngine
from openhands_connect.redaction import Redactor
from openhands_connect.service import ConnectService

AUDIT_LOG_ENV_VAR = "OH_CONNECT_AUDIT_LOG"


def build_service() -> ConnectService:
    config = load_config()
    credential_values = tuple(
        value for env in config.environments if (value := _credential(config, env))
    )
    redactor = Redactor(credential_values)
    provider = OpenHandsAdapter(
        lambda environment: _credential(config, environment), redactor=redactor
    )
    audit_path = os.environ.get(AUDIT_LOG_ENV_VAR)
    audit = FileAuditSink(audit_path) if audit_path else StreamAuditSink()
    return ConnectService(
        registry=EnvironmentRegistry(config.environments),
        provider=provider,
        policy=PolicyEngine(),
        audit=audit,
    )


def _credential(config: LoadedConfig, environment: Environment) -> str | None:
    try:
        return config.credential_for(environment)
    except ConfigurationError:
        # A missing credential is reported when the environment is actually
        # used, so listing environments still works with an incomplete setup.
        return None


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
