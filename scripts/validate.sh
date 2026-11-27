#!/usr/bin/env bash
# Local validation entry point.
#
# Runs deterministic checks only: no network, no OpenHands server required.
# The opt-in integration tests stay skipped unless OH_CONNECT_INTEGRATION_BASE_URL
# is set, so this script is safe to run anywhere.
#
# Usage: ./scripts/validate.sh [--with-integration]
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

failures=0

step() {
  local name="$1"
  shift
  printf '\n\033[1m==> %s\033[0m\n' "$name"
  if "$@"; then
    printf '\033[32mPASS\033[0m %s\n' "$name"
  else
    printf '\033[31mFAIL\033[0m %s\n' "$name"
    failures=$((failures + 1))
  fi
}

step "ruff lint" uv run ruff check src/
step "ruff format check" uv run ruff format --check src/
step "mypy (strict)" uv run mypy
step "pytest" uv run pytest -m "not integration"

if [ "${1:-}" = "--with-integration" ]; then
  if [ -z "${OH_CONNECT_INTEGRATION_BASE_URL:-}" ]; then
    echo
    echo "error: --with-integration needs OH_CONNECT_INTEGRATION_BASE_URL" >&2
    echo "       (and OH_CONNECT_INTEGRATION_API_KEY if the server requires auth)" >&2
    exit 1
  fi
  step "pytest (integration)" uv run pytest -m integration
fi

echo
if [ "$failures" -ne 0 ]; then
  printf '\033[31m%d check(s) failed\033[0m\n' "$failures"
  exit 1
fi
printf '\033[32mAll checks passed\033[0m\n'
