"""User path normalisation, URL encoding and ETag helpers."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit

from fastmcp.exceptions import ToolError

MAX_PATH_LENGTH = 1024


# Bidirectional embedding/override (U+202A-U+202E) and isolate (U+2066-U+2069) controls can
# make a path display differently from what it is ("Trojan Source" style spoofing).
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def _has_control(text: str) -> bool:
    """C0 controls, DEL and C1 controls (U+0080-U+009F)."""
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in text)


def _is_etag_char(ch: str) -> bool:
    """RFC 9110 etagc: %x21 / %x23-7E (obs-text is not accepted here)."""
    return ch == "!" or "#" <= ch <= "~"


def normalize_path(path: str) -> str:
    """Normalise a user path (relative to the files home).

    Returns the un-encoded path without a leading slash; ``""`` is the home folder.
    Raises :class:`ToolError` for paths that are ambiguous or unsafe.
    """
    if not isinstance(path, str):
        raise ToolError("path must be a string.")
    if len(path) > MAX_PATH_LENGTH:
        raise ToolError(f"path is longer than {MAX_PATH_LENGTH} characters.")
    if _has_control(path):
        raise ToolError("path must not contain control characters.")
    if any(ch in _BIDI_CONTROLS for ch in path):
        raise ToolError("path must not contain bidirectional override or isolate characters.")
    if "\\" in path:
        raise ToolError("path must use '/' as separator, not backslashes.")
    if path in ("", "/"):
        return ""
    segments = path.split("/")
    if segments and segments[0] == "":
        segments = segments[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]
    for segment in segments:
        if segment == "":
            raise ToolError(f'path "{path}" contains an empty segment ("//").')
        if segment in (".", ".."):
            raise ToolError(f'path "{path}" must not contain "." or ".." segments.')
    return "/".join(segments)


def encode_path(normalized: str) -> str:
    """Percent-encode every segment of a normalised path; nothing is left unescaped."""
    if normalized == "":
        return ""
    return "/".join(quote(segment, safe="") for segment in normalized.split("/"))


def display_path(normalized: str) -> str:
    """Path as shown in messages: the home folder is shown as ``/``."""
    return normalized if normalized else "/"


def href_to_path(href: str, home_path: str) -> str | None:
    """Turn a ``<d:href>`` into a user path, or ``None`` when it is outside the home.

    ``home_path`` is the (encoded) URL path of the files home, ending with ``/``.
    """
    try:
        href_path = unquote(urlsplit(href).path)
    except ValueError:  # e.g. "http://[bad/x": treated like an href outside the home
        return None
    home = unquote(home_path)
    if not home.endswith("/"):
        home += "/"
    if href_path.rstrip("/") == home.rstrip("/"):
        return ""
    if not href_path.startswith(home):
        return None
    return href_path[len(home) :].strip("/")


# Apache's mod_deflate (and mod_brotli) add the content coding to the ETag of a compressed
# response ("abc" becomes "abc-gzip": DeflateAlterETag AddSuffix is the default), so the ETag
# a GET of a compressed text file (in practice every one from about 1 KB up) returns is one
# that neither If-Match nor a PROPFIND getetag will ever show. Nextcloud's own ETags are plain
# hex strings: the suffix is never part of one.
_CODING_SUFFIXES = ("-gzip", "-br", "-deflate", "-zstd")


def normalize_etag(etag: str | None) -> str | None:
    """Return an ETag without ``W/``, quotes and content-coding suffix (``None`` stays ``None``)."""
    if etag is None:
        return None
    value = etag.strip()
    if value.startswith(("W/", "w/")):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    for suffix in _CODING_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            value = value[: -len(suffix)]
            break
    return value or None


def caller_etag(etag: str | None, argument: str = "expected_etag") -> str:
    """Validate an ETag given by the caller and return it normalised."""
    value = normalize_etag(etag)
    if value is None:
        raise ToolError(f"{argument} must not be empty; read the file first to get its etag.")
    if len(value) > 256 or not all(_is_etag_char(ch) for ch in value):
        raise ToolError(
            f"{argument} is not a valid etag; copy it exactly as a read tool returned it."
        )
    return value


def display_etag(etag: str | None) -> str | None:
    """A server etag that may be shown in a message (RFC 9110 etagc ASCII, <= 256 chars)."""
    if not etag or len(etag) > 256 or not all(_is_etag_char(ch) for ch in etag):
        return None
    return etag


def quote_etag(etag: str) -> str:
    """Format an ETag for ``If-Match``."""
    return f'"{etag}"'
