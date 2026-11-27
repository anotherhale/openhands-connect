"""Audit sink.

Writes metadata-only JSON lines. ``AuditEvent`` has no field that can carry a
prompt, task output, or credential, so this store cannot leak them even if a
caller passes one by mistake.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import IO, Protocol

from openhands_connect.domain.models import AuditEvent

logger = logging.getLogger(__name__)


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    """Discards events. Used by tests that do not assert on audit output."""

    def record(self, event: AuditEvent) -> None:
        return None


class StreamAuditSink:
    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def record(self, event: AuditEvent) -> None:
        # Audit output must never break a tool call; stderr keeps it out of the
        # MCP stdio channel, which carries protocol traffic. A write failure is
        # warned about rather than swallowed, so a broken audit trail is visible.
        try:
            self._stream.write(event.model_dump_json() + "\n")
            self._stream.flush()
        except (OSError, ValueError) as exc:
            logger.warning("audit event could not be written: %s", type(exc).__name__)


class FileAuditSink:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: AuditEvent) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.model_dump(mode="json")) + "\n")
        except OSError as exc:
            logger.warning(
                "audit event could not be appended to %s: %s",
                self._path,
                type(exc).__name__,
            )
