"""Headscale deployment policy for the shared admin session cookie."""
import os


def secure_cookie_enabled() -> bool:
    value = os.environ.get("HEADSCALE_MCP_SECURE_COOKIE", "true").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError("HEADSCALE_MCP_SECURE_COOKIE must be true or false")
    return value == "true"


def secure_session_cookie(response) -> None:
    if not secure_cookie_enabled():
        return
    headers = []
    for name, value in response.raw_headers:
        if (name.lower() == b"set-cookie"
                and value.split(b"=", 1)[0].strip() == b"mcp-admin-token"
                and not any(part.strip().lower() == b"secure" for part in value.split(b";")[1:])):
            value += b"; Secure"
        headers.append((name, value))
    response.raw_headers = headers
