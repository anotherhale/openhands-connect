"""Errors raised by the core, distinct from provider errors.

The MCP layer maps these to safe, actionable tool errors. Messages here must
never contain credentials or raw provider payloads.
"""

from __future__ import annotations


class ConnectError(Exception):
    """Base class for all errors the bridge reports to a caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class UnknownEnvironmentError(ConnectError):
    """The requested environment ID is not configured."""


class PolicyDeniedError(ConnectError):
    """Policy refused the operation for the selected environment."""


class InvalidInputError(ConnectError):
    """A caller-supplied value failed validation beyond schema checks."""


class ConfigurationError(ConnectError):
    """Configuration is missing, unreadable, or invalid.

    Not a provider error: nothing was sent to a server.
    """
