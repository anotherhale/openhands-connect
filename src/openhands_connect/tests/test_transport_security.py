"""Remote transport security and per-environment isolation (issue #9).

A remote environment must be reached over TLS. Credentials travel in the
``X-Session-API-Key`` header, so a plaintext remote URL would put a token on the
wire in the clear. Loopback is exempt because development servers listen on
plain HTTP by convention and the traffic never leaves the machine.
"""

from __future__ import annotations

import httpx
import pytest

from openhands_connect.adapters.openhands import OpenHandsAdapter
from openhands_connect.config import load_config
from openhands_connect.domain.errors import ConfigurationError
from openhands_connect.domain.models import Environment, ProviderKind
from openhands_connect.tests.conftest import VALID_CONFIG


def _config_with(entries: str) -> str:
    return f"schema_version: 1\nenvironments:\n{entries}"


def _entry(env_id: str, base_url: str, *, credential: str | None = None) -> str:
    lines = [
        f"  - id: {env_id}",
        f"    name: {env_id} env",
        "    provider: openhands",
        f"    base_url: {base_url}",
        "    allowed_operations:",
        "      - get_task",
    ]
    if credential:
        lines.append(f"    credential_env_var: {credential}")
    return "\n".join(lines) + "\n"


# --- What must be rejected ---------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://agent.example.com:18000",  # remote over plaintext
        "http://10.0.0.5:18000",  # private network, still not loopback
        "http://192.168.1.10:18000",  # private network
        "http://agent.example.com",  # default port, still plaintext remote
        "ftp://agent.example.com",  # not an HTTP transport at all
        "file:///etc/passwd",  # local file read
        "not-even-a-url",  # no scheme, no host
        "",
    ],
)
def test_a_remote_environment_must_use_https(config_file, base_url: str) -> None:
    """Plaintext, non-HTTP, and malformed transports are configuration errors."""
    path = config_file(_config_with(_entry("remote", base_url)))

    with pytest.raises(ConfigurationError) as excinfo:
        load_config(path, env={})

    message = str(excinfo.value)
    assert "base_url" in message or "remote" in message
    # The rejected URL is echoed back so the operator can fix it, but a config
    # error must never contain a credential.
    assert "token" not in message.lower()


def test_rejection_names_the_problem_and_stays_actionable(config_file) -> None:
    """A plaintext remote URL must say *why* and what to do instead."""
    path = config_file(_config_with(_entry("remote", "http://agent.example.com")))

    with pytest.raises(ConfigurationError) as excinfo:
        load_config(path, env={})

    message = str(excinfo.value).lower()
    assert "https" in message
    assert "remote" in message or "remote" in str(excinfo.value)


# --- What must be allowed ----------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "https://agent.example.com",
        "https://agent.example.com:8443",
        "https://agent.example.com/base/path",
        "http://127.0.0.1:18000",
        "http://localhost:18000",
        "http://[::1]:18000",
    ],
)
def test_https_remote_and_plaintext_loopback_are_accepted(config_file, base_url: str) -> None:
    path = config_file(_config_with(_entry("env1", base_url)))

    loaded = load_config(path, env={})

    assert loaded.environments[0].base_url == base_url.rstrip("/")


def test_the_existing_local_config_still_loads(config_file) -> None:
    """The shipped loopback example must not be broken by the new rule."""
    loaded = load_config(config_file(VALID_CONFIG), env={"OH_LOCAL_KEY": "token-abc"})

    assert loaded.environments[0].base_url == "http://127.0.0.1:18000"


def test_a_url_without_a_host_is_rejected(config_file) -> None:
    """No netloc means nothing to connect to; reject on that ground specifically."""
    for base_url in ("https://", "http://", "http:///path"):
        path = config_file(
            _config_with(_entry("broken", base_url)), name=f"n{abs(hash(base_url))}.yaml"
        )
        with pytest.raises(ConfigurationError, match="absolute URL"):
            load_config(path, env={})


@pytest.mark.parametrize("base_url", ["ftp://127.0.0.1:18000", "ws://localhost:18000"])
def test_a_loopback_url_with_a_non_http_scheme_is_still_rejected(
    config_file, base_url: str
) -> None:
    """The loopback exemption covers plaintext HTTP only, not any scheme.

    Without this, a non-HTTP scheme on a loopback host would slip through on the
    loopback allowance alone.
    """
    path = config_file(_config_with(_entry("odd", base_url)), name=f"s{abs(hash(base_url))}.yaml")
    with pytest.raises(ConfigurationError, match="scheme"):
        load_config(path, env={})


def test_a_url_embedding_credentials_is_rejected_without_echoing_them(config_file) -> None:
    """A password in a URL must be refused *and* must not appear in the error.

    ``base_url`` is repeated in the configuration error, which reaches tool
    output. Two independent guards keep the secret out: the validator echoes
    only the scheme and host, and the redactor strips URL userinfo at the MCP
    boundary as defence in depth. This test pins the first.
    """
    secret = "sup3r-s3cr3t-value"
    path = config_file(_config_with(_entry("leaky", f"https://user:{secret}@host.example")))

    with pytest.raises(ConfigurationError) as excinfo:
        load_config(path, env={})

    message = str(excinfo.value)
    assert secret not in message
    assert "credential" in message.lower()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://[::1",  # malformed IPv6 literal: urlsplit raises and quotes it
        "http://user:hunter2@[::1",
        "not-a-url",
        "user:hunter2@host.example",
    ],
)
def test_a_malformed_url_never_echoes_its_input(config_file, base_url: str) -> None:
    """A parser rejection must produce our own message, never a raw one.

    ``urlsplit`` raises ``Invalid IPv6 URL`` for a malformed literal. Its text
    happens not to contain the input on current Python, but that is not a
    contracted guarantee, so the validator substitutes its own wording. The test
    holds either way: the error that reaches a caller carries no user input.
    """
    path = config_file(_config_with(_entry("broken", base_url)), name="malformed.yaml")

    with pytest.raises(ConfigurationError) as excinfo:
        load_config(path, env={})

    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "sup3r" not in message
    # Our own wording, not the parser's: "Invalid IPv6 URL" would mean the raw
    # stdlib message reached the caller.
    assert "invalid ipv6 url" not in message.lower()
    assert "expected https://host" in message


def test_lookalike_hostnames_are_not_treated_as_loopback(config_file) -> None:
    """A hostile host must not pass the loopback exemption by name similarity.

    ``localhost.attacker.example`` and ``127.0.0.1.nip.io`` are remote hosts; a
    substring or prefix check would wrongly exempt them.
    """
    for host in ("http://localhost.attacker.example", "http://127.0.0.1.nip.io:8000"):
        path = config_file(_config_with(_entry("remote", host)), name=f"c{abs(hash(host))}.yaml")
        with pytest.raises(ConfigurationError):
            load_config(path, env={})


# --- Failure classification for a remote environment -------------------------


def test_transport_failures_are_classified_distinctly() -> None:
    """A remote operator must be able to tell a TLS problem from a DNS one.

    All of these arrive as ``ConnectError``; reporting only the exception class
    hides which of them it was, which is the commonest remote setup mistake.
    """
    from openhands_connect.adapters.openhands import _transport_detail

    def request() -> httpx.Request:
        return httpx.Request("GET", "https://agent.example.com/server_info")

    tls = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed",
        request=request(),
    )
    dns = httpx.ConnectError(
        "[Errno 8] nodename nor servname provided, or not known", request=request()
    )
    refused = httpx.ConnectError("[Errno 61] Connection refused", request=request())

    tls_detail = _transport_detail(tls)
    dns_detail = _transport_detail(dns)
    refused_detail = _transport_detail(refused)

    assert "tls" in tls_detail.lower() or "certificate" in tls_detail.lower()
    assert "resolved" in dns_detail.lower() or "dns" in dns_detail.lower()
    assert "listening" in refused_detail.lower()
    assert len({tls_detail, dns_detail, refused_detail}) == 3
    # The upstream wording must not be echoed back: these tests double as proof
    # that classification does not simply relay the original exception text.
    for detail in (tls_detail, dns_detail, refused_detail):
        assert "errno" not in detail.lower()


def test_a_minimal_tls_failure_is_still_classified_as_tls() -> None:
    """The only TLS signal may be the word "tls" itself.

    Without this, dropping the TLS branch goes unnoticed: a realistic OpenSSL
    message also contains "certificate" and "self-signed", so other needles in
    the same group still match and the mutant survives.
    """
    from openhands_connect.adapters.openhands import _transport_detail

    detail = _transport_detail(httpx.ConnectError("TLS handshake failure"))

    assert "tls" in detail.lower()


def test_an_unrecognised_exception_does_not_echo_its_class_name() -> None:
    """The fallback must not repeat an arbitrary class name to a caller.

    A subclass could carry a credential-shaped name; only known-safe names are
    repeated verbatim.
    """
    from openhands_connect.adapters.openhands import _transport_detail

    class S3cr3tPwTransportBoom(httpx.HTTPError):
        pass

    detail = _transport_detail(S3cr3tPwTransportBoom("boom"))

    assert "s3cr3tpwtransportboom" not in detail.lower()
    assert "connection failed" in detail.lower()


def test_transport_classification_does_not_leak_the_hostname() -> None:
    """Classification may name the failure kind, never the address it dialled.

    The original ``httpx`` text can contain a host, a URL or a header value, so
    only a fixed phrase per failure kind is reported.
    """
    from openhands_connect.adapters.openhands import _transport_detail

    secret_host = "internal-db-7.corp.example"
    exc = httpx.ConnectError(
        f"[Errno 8] nodename nor servname provided: {secret_host}", request=None
    )

    detail = _transport_detail(exc)

    assert secret_host not in detail
    assert "resolved" in detail.lower() or "dns" in detail.lower()


# --- Per-environment isolation ----------------------------------------------


def test_two_environments_are_listed_and_addressed_independently(config_file) -> None:
    body = _config_with(
        _entry("local", "http://127.0.0.1:18000", credential="OH_KEY_LOCAL")
        + _entry("remote", "https://agent.example.com", credential="OH_KEY_REMOTE")
    )
    loaded = load_config(
        config_file(body),
        env={"OH_KEY_LOCAL": "token-local", "OH_KEY_REMOTE": "token-remote"},
    )

    by_id = {env.id: env for env in loaded.environments}

    assert sorted(by_id) == ["local", "remote"]
    assert by_id["local"].base_url == "http://127.0.0.1:18000"
    assert by_id["remote"].base_url == "https://agent.example.com"
    assert loaded.credential_for(by_id["local"]) == "token-local"
    assert loaded.credential_for(by_id["remote"]) == "token-remote"


# --- Two environments: routing and credential isolation ----------------------

_TWO_ENV_CONFIG = """\
schema_version: 1
environments:
  - id: local
    name: Local
    provider: openhands
    base_url: http://127.0.0.1:18000
    credential_env_var: OH_KEY_LOCAL
    allowed_operations: [get_task]
  - id: remote
    name: Remote
    provider: openhands
    base_url: https://agent.example.com
    credential_env_var: OH_KEY_REMOTE
    allowed_operations: [get_task]
"""


def _two_env_adapter(
    tmp_path, seen: list[tuple[str, str | None]]
) -> tuple[OpenHandsAdapter, dict[str, Environment]]:
    """Build the real config -> adapter wiring, with only the socket faked.

    This mirrors ``build_service``: credentials come from
    ``LoadedConfig.credential_for``, the same callable production uses, so a
    credential mixed up between environments fails here exactly as it would in
    the server.
    """
    path = tmp_path / "environments.yaml"
    path.write_text(_TWO_ENV_CONFIG, encoding="utf-8")
    loaded = load_config(
        path,
        env={"OH_KEY_LOCAL": "local-token-aaa", "OH_KEY_REMOTE": "remote-token-bbb"},
    )
    by_id = {env.id: env for env in loaded.environments}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("X-Session-API-Key")))
        # A task status both hosts can answer.
        return httpx.Response(
            200,
            json={
                "id": "t1",
                "status": "finished",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )

    adapter = OpenHandsAdapter(
        lambda env: loaded.credential_for(env),
        transport=httpx.MockTransport(handler),
    )
    return adapter, by_id


async def test_each_environment_dials_its_own_host_with_its_own_credential(tmp_path) -> None:
    """The core promise of multi-environment support, checked end to end.

    Two environments, two hosts, two tokens. Each call must reach the host it
    named and carry only that environment's token: mixing them up would send one
    environment's secret to another's server, which is precisely the failure this
    issue exists to prevent.
    """
    seen: list[tuple[str, str | None]] = []
    adapter, by_id = _two_env_adapter(tmp_path, seen)

    await adapter.get_task(by_id["local"], "t1")
    await adapter.get_task(by_id["remote"], "t1")

    assert seen == [
        ("127.0.0.1", "local-token-aaa"),
        ("agent.example.com", "remote-token-bbb"),
    ]


async def test_an_environment_without_its_credential_fails_alone(tmp_path) -> None:
    """A missing token must not borrow the other environment's.

    The registry has no fallback, and the credential provider is asked per
    environment, so a half-configured setup fails closed on the environment that
    is incomplete and leaves the healthy one working.
    """
    path = tmp_path / "environments.yaml"
    path.write_text(_TWO_ENV_CONFIG, encoding="utf-8")
    # Only the local key is present.
    loaded = load_config(path, env={"OH_KEY_LOCAL": "local-token-aaa"})
    by_id = {env.id: env for env in loaded.environments}

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.headers.get("X-Session-API-Key", ""))
        return httpx.Response(
            200,
            json={
                "id": "t1",
                "status": "finished",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )

    adapter = OpenHandsAdapter(
        lambda env: loaded.credential_for(env), transport=httpx.MockTransport(handler)
    )

    # The healthy environment still works...
    await adapter.get_task(by_id["local"], "t1")
    assert requests == ["local-token-aaa"]

    # ...and the incomplete one fails closed instead of using the local token.
    # A missing credential is an operator fault, not an upstream failure, so it
    # surfaces as ConfigurationError (audited by the service layer) rather than
    # a provider error.
    with pytest.raises(ConfigurationError) as excinfo:
        await adapter.get_task(by_id["remote"], "t1")

    assert requests == ["local-token-aaa"], "no request may be sent for a missing credential"
    assert "OH_KEY_REMOTE" in str(excinfo.value)
    assert "local-token-aaa" not in str(excinfo.value)


def test_one_missing_credential_does_not_affect_the_other_environment(config_file) -> None:
    """Credential resolution is per environment, with no cross-fill and no fallback."""
    body = _config_with(
        _entry("local", "http://127.0.0.1:18000", credential="OH_KEY_LOCAL")
        + _entry("remote", "https://agent.example.com", credential="OH_KEY_REMOTE")
    )
    loaded = load_config(
        config_file(body),
        env={"OH_KEY_LOCAL": "token-local"},
    )
    by_id = {env.id: env for env in loaded.environments}

    assert loaded.credential_for(by_id["local"]) == "token-local"
    with pytest.raises(ConfigurationError, match="OH_KEY_REMOTE"):
        loaded.credential_for(by_id["remote"])


def test_an_environment_without_a_credential_does_not_borrow_one(config_file) -> None:
    body = _config_with(
        _entry("local", "http://127.0.0.1:18000", credential="OH_KEY_LOCAL")
        + _entry("anon", "https://agent.example.com")
    )
    loaded = load_config(config_file(body), env={"OH_KEY_LOCAL": "token-local"})
    by_id = {env.id: env for env in loaded.environments}

    assert loaded.credential_for(by_id["anon"]) is None


def test_duplicate_environment_ids_are_rejected() -> None:
    from openhands_connect.domain.registry import EnvironmentRegistry

    env = Environment(
        id="dup",
        name="dup",
        provider=ProviderKind.OPENHANDS,
        base_url="https://agent.example.com",
    )
    with pytest.raises(ValueError, match="duplicate environment ids"):
        EnvironmentRegistry((env, env))


# --- Unreachable remote: explicit failure, no failover -----------------------


@pytest.mark.asyncio
async def test_an_unreachable_remote_is_a_clear_failure_not_a_fallback() -> None:
    """A dead remote reports its own failure; it never silently uses another env."""
    from openhands_connect.adapters.openhands import OpenHandsAdapter

    remote = Environment(
        id="remote",
        name="Remote",
        provider=ProviderKind.OPENHANDS,
        base_url="https://agent.example.com",
        credential_env_var=None,
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = OpenHandsAdapter(
        credential_provider=lambda env: None,
        transport=httpx.MockTransport(refuse),
    )

    health = await adapter.health_check(remote)

    assert health.reachable is False
    # The failure names the environment it belongs to, so an operator with many
    # configured environments knows which one is down.
    assert "remote" in health.detail
