"""Redaction of credentials from anything that may reach a caller.

Applied at the adapter boundary (error messages) and again at the MCP boundary
(tool payloads). Redaction is idempotent, so applying it twice is safe.
"""

from __future__ import annotations

import re
from typing import Any, overload

REDACTED = "[REDACTED]"

_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|credential|authorization|cookie)"
)
"""A mapping key that names a credential; its value is redacted wholesale."""

#: A value already replaced by this module must never be matched again, or a
#: second pass would append a stray bracket to ``[REDACTED]``. Enforced in the
#: replacement function rather than with a lookahead, because the pattern can
#: backtrack past a lookahead and re-match the marker.
_HEADER_PATTERN = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-session-api-key|x-api-key|"
    r"api[_-]?key|access[_-]?token|secret[_-]?key|bearer|token)"
    r"(\s*[:=]\s*|\s+)(?:(bearer\s+))?((?![Bb]earer\b)[^\s,;\"'\[\]}\)]+)"
)

_JSON_SECRET_PATTERN = re.compile(
    r"(?i)(\"[a-z0-9_-]*(?:api[_-]?key|token|secret|password|credential)"
    r"[a-z0-9_-]*\"\s*:\s*)\"([^\"]*)\""
)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),
)


def _redact_header(match: re.Match[str]) -> str:
    name, separator, scheme, value = match.groups()
    if value == REDACTED:
        return match.group(0)
    return f"{name}{separator}{scheme or ''}{REDACTED}"


def _redact_json_secret(match: re.Match[str]) -> str:
    prefix, value = match.groups()
    if value == REDACTED:
        return match.group(0)
    return f'{prefix}"{REDACTED}"'


class Redactor:
    """Redacts known literal secrets, then applies generic patterns.

    Literal secrets come from the environment's credential and are replaced
    first so a token that happens not to match a pattern is still removed.
    """

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        # Longest first, so a secret that contains another is fully removed.
        self._literals = tuple(sorted((s for s in secrets if s), key=len, reverse=True))

    def redact(self, text: str) -> str:
        if not text:
            return text
        result = text
        for secret in self._literals:
            if secret in result:
                result = result.replace(secret, REDACTED)
        result = _HEADER_PATTERN.sub(_redact_header, result)
        result = _JSON_SECRET_PATTERN.sub(_redact_json_secret, result)
        for pattern, replacement in _SECRET_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    @overload
    def redact_mapping(self, value: dict[str, Any]) -> dict[str, Any]: ...
    @overload
    def redact_mapping(self, value: object) -> object: ...
    def redact_mapping(self, value: object) -> object:
        """Recursively redact strings inside dicts, lists and tuples.

        A value under a credential-named key (``X-Session-API-Key``,
        ``api_key``, …) is replaced wholesale, since a bare token often matches
        no pattern.
        """
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            redacted: dict[object, object] = {}
            for key, item in value.items():
                if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                    redacted[key] = REDACTED if item else item
                else:
                    redacted[key] = self.redact_mapping(item)
            return redacted
        if isinstance(value, (list, tuple)):
            return [self.redact_mapping(item) for item in value]
        return value


def redact(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    return Redactor(secrets).redact(text)
