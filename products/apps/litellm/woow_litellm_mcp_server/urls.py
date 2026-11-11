"""LiteLLM gateway roots; a prefixed /v1 remains a reverse-proxy mount."""
from urllib.parse import urlsplit, urlunsplit


def normalize_base_url(raw: str) -> str:
    """Remove an exact origin-root /v1 only; do not guess at prefixed paths.

    Empty is allowed for an unconfigured admin form. URL validation here is
    product-specific and does not change any other product's URL contract.
    """
    url = raw.strip().rstrip("/")
    if not url:
        return ""
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("invalid LiteLLM base URL")
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or "?" in url or "#" in url):
        raise ValueError("expected HTTP(S) LiteLLM base URL without credentials/query/fragment")
    _ = parts.port
    path = "" if parts.path == "/v1" else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
