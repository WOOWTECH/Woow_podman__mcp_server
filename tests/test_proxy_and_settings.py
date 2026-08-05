"""Regressions for the proxy Host header and the TLS-path secret heuristic.

Both bugs are Podman-specific consequences of code that was correct enough for
LiteLLM, which is exactly the class of thing a fork silently inherits.
"""

from __future__ import annotations

import pytest

from mcp_admin_core.routers.settings import _is_secret_key, _merge_preserving_secrets


# -- Host header: the 421 that broke every connector call --------------------


def test_proxy_forwards_upstream_authority_not_bare_localhost() -> None:
    """The proxy must send ``127.0.0.1:<port>``, with the port.

    The MCP SDK auto-enables DNS-rebinding protection for a loopback bind with
    ``allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]``.  Every pattern
    there requires a colon, so the reference's port-less ``"localhost"`` matched
    none of them and the child answered 421 Misdirected Request to every
    ``tools/list``.  Asserted against the source because the header is built
    inline in the request path.
    """
    import inspect

    from mcp_admin_core import proxy

    source = inspect.getsource(proxy.mcp_proxy)
    assert 'forward_headers["host"] = f"127.0.0.1:{mcp_port}"' in source
    assert 'forward_headers["host"] = "localhost"' not in source


def test_sdk_default_allowed_hosts_all_require_a_port() -> None:
    """Pin the assumption the fix rests on.

    If a future SDK adds a port-less entry this test still passes; if it
    *removes* the wildcard-port forms, the fix above needs revisiting and this
    is where that shows up.
    """
    from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

    middleware = TransportSecurityMiddleware(
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
        )
    )
    assert middleware._validate_host("127.0.0.1:8000") is True
    assert middleware._validate_host("localhost") is False, (
        "a port-less Host is rejected — this is the bug the proxy fix avoids"
    )


# -- TLS paths are not secrets -----------------------------------------------


@pytest.mark.parametrize("key", ["podman_tls_ca", "podman_tls_cert", "podman_tls_key"])
def test_tls_paths_are_not_treated_as_secrets(key: str) -> None:
    """``..._key``/``..._cert`` match the substring heuristic but are paths."""
    assert _is_secret_key(key) is False


@pytest.mark.parametrize("key", ["podman_api_key", "mcp_auth_token", "admin_password", "my_secret"])
def test_real_secrets_still_match(key: str) -> None:
    assert _is_secret_key(key) is True


def test_tls_path_can_be_cleared() -> None:
    """The GUI must be able to blank a TLS path once it is set.

    ``_merge_preserving_secrets`` skips an empty string for a secret-looking key
    (that is how "leave blank to keep the stored value" works).  Applied to a
    file path it meant the field could be set but never unset — the operator
    switched a host from mTLS to plain TCP and the stale cert path kept being
    passed to the child.
    """
    stored = {"podman_uri": "tcp://host:2376", "podman_tls_cert": "/certs/old.pem"}
    merged = _merge_preserving_secrets(stored, {"podman_tls_cert": ""}, section="connection")
    assert merged["podman_tls_cert"] == ""


def test_real_secret_in_connection_is_still_preserved_when_blank() -> None:
    """The behaviour the exemption must not break.

    The blank-means-untouched rule is scoped to the ``connection`` section, so
    that is where the exemption has to be surgical: a genuine credential there
    still survives an empty PUT, while the TLS paths above do not.
    """
    stored = {"podman_api_key": "the-real-token"}
    merged = _merge_preserving_secrets(stored, {"podman_api_key": ""}, section="connection")
    assert merged["podman_api_key"] == "the-real-token"


def test_masked_echo_never_overwrites_the_stored_secret() -> None:
    """The GUI re-posts what GET showed it; that must be a no-op."""
    from mcp_admin_core.config.store import mask_secret

    stored = {"podman_api_key": "super-secret-value"}
    echo = mask_secret(stored["podman_api_key"])
    merged = _merge_preserving_secrets(stored, {"podman_api_key": echo}, section="connection")
    assert merged["podman_api_key"] == "super-secret-value"
