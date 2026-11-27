"""Audit sink behaviour: metadata-only output and visible write failures."""

from __future__ import annotations

import io
import logging

import pytest

from openhands_connect.audit import StreamAuditSink
from openhands_connect.domain.models import AuditEvent, AuditOutcome, Operation


class _BrokenStream(io.StringIO):
    def write(self, _text: str) -> int:
        raise OSError("disk full")


def _event() -> AuditEvent:
    return AuditEvent(
        environment_id="local",
        operation=Operation.GET_TASK,
        outcome=AuditOutcome.FAILED,
    )


def test_stream_sink_writes_a_json_line() -> None:
    stream = io.StringIO()

    StreamAuditSink(stream).record(_event())

    written = stream.getvalue()
    assert written.endswith("\n")
    assert '"environment_id":"local"' in written


def test_stream_sink_warns_instead_of_raising_on_write_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken audit stream must not break a tool call, but must be visible."""
    with caplog.at_level(logging.WARNING, logger="openhands_connect.audit"):
        StreamAuditSink(_BrokenStream()).record(_event())

    assert any("audit event could not be written" in record.message for record in caplog.records)
