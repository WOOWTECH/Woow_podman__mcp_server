"""WebDAV / CalDAV request bodies and multistatus parsing (RFC 4918, RFC 4791)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from fastmcp.exceptions import ToolError

from .errors import INVALID_RESPONSE, SAFE_QUOTA_DIAGNOSTIC, with_code
from .paths import normalize_etag

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CALSERVER = "http://calendarserver.org/ns/"
APPLE_ICAL = "http://apple.com/ns/ical/"
SABRE = "http://sabredav.org/ns"


def tag(namespace: str, name: str) -> str:
    """Clark notation ``{namespace}name``."""
    return f"{{{namespace}}}{name}"


FILE_PROPFIND = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    b"<d:getetag/><d:getcontenttype/>"
    b"</d:prop></d:propfind>"
)

ETAG_PROPFIND = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>'
)

CALENDAR_PROPFIND = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'
    b' xmlns:cs="http://calendarserver.org/ns/" xmlns:a="http://apple.com/ns/ical/">'
    b"<d:prop><d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>"
    b"<a:calendar-color/><d:current-user-privilege-set/><cs:getctag/></d:prop>"
    b"</d:propfind>"
)

TODO_REPORT = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    b"<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    b'<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VTODO"/>'
    b"</c:comp-filter></c:filter>"
    b"</c:calendar-query>"
)


@dataclass
class DavResponse:
    """One ``<d:response>`` of a multistatus: href, status and the props found (200 only)."""

    href: str
    status: int | None = None
    props: dict[str, Element] = field(default_factory=dict)

    def text(self, name: str) -> str | None:
        element = self.props.get(name)
        if element is None:
            return None
        value = (element.text or "").strip()
        return value or None

    def has_child(self, prop: str, child: str) -> bool:
        element = self.props.get(prop)
        return element is not None and element.find(child) is not None


def _status_code(text: str | None) -> int | None:
    # "HTTP/1.1 200 OK"
    if not text:
        return None
    parts = text.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def parse_xml(body: bytes) -> Element:
    """Parse untrusted XML with defusedxml (no DTDs, no entities, no external refs)."""
    try:
        return SafeET.fromstring(body, forbid_dtd=True)
    except (DefusedXmlException, SafeET.ParseError, ValueError, LookupError) as exc:
        # LookupError: an XML declaration naming an unknown encoding (e.g. "x-bogus")
        raise with_code(
            ToolError("Nextcloud sent an answer that is not valid WebDAV XML."), INVALID_RESPONSE
        ) from exc


def parse_multistatus(body: bytes) -> list[DavResponse]:
    """Parse a ``207 Multi-Status`` body into :class:`DavResponse` objects.

    Responses with a non-2xx response-level status are left out; within a response only
    2xx propstats contribute properties.
    """
    root = parse_xml(body)
    if root.tag != tag(DAV, "multistatus"):
        raise with_code(
            ToolError("Nextcloud sent an answer that is not a WebDAV multistatus."),
            INVALID_RESPONSE,
        )
    results: list[DavResponse] = []
    for response in root.findall(tag(DAV, "response")):
        href = (response.findtext(tag(DAV, "href")) or "").strip()
        if not href:
            continue
        status = _status_code(response.findtext(tag(DAV, "status")))
        if status is not None and not 200 <= status < 300:
            # e.g. a member that vanished (404) or cannot be read (403): not a resource here
            continue
        item = DavResponse(href=href, status=status)
        for propstat in response.findall(tag(DAV, "propstat")):
            code = _status_code(propstat.findtext(tag(DAV, "status")))
            prop = propstat.find(tag(DAV, "prop"))
            if prop is None:
                continue
            if code is not None and not 200 <= code < 300:
                continue
            if item.status is None:
                item.status = code
            for child in prop:
                item.props[child.tag] = child
        results.append(item)
    return results


_QUOTA_EXHAUSTED = re.compile(
    r"\bquota\s+(?:(?:is\s+)?(?:exhausted|exceeded)|of\s+the\s+home\s+folder\s+is\s+exhausted)\b",
    re.I,
)


def safe_server_diagnostic(body: bytes, content_type: str | None = None) -> str | None:
    """Return a fixed diagnostic category, never untrusted backend text.

    Error bodies may contain credentials that are not known to this process, including
    tokens and internal addresses. No pattern-based redactor can guarantee their removal.
    Only a bounded, recognized condition is allowed into a public tool error.
    """
    if not body or len(body) > 65536:
        return None
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    start = body.lstrip()[:1]
    message: str | None = None
    if media_type in ("application/xml", "text/xml") or (not media_type and start == b"<"):
        try:
            root = SafeET.fromstring(body, forbid_dtd=True)
            if root.tag == tag(DAV, "error"):
                message = root.findtext(tag(SABRE, "message"))
        except (DefusedXmlException, SafeET.ParseError, ValueError, LookupError):
            return None
    elif (
        media_type == "application/json"
        or media_type.endswith("+json")
        or (not media_type and start == b"{")
    ):
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                ocs = payload.get("ocs")
                if isinstance(ocs, dict):
                    meta = ocs.get("meta")
                    if isinstance(meta, dict) and isinstance(meta.get("message"), str):
                        message = meta["message"]
        except (UnicodeError, ValueError):
            return None
    elif media_type == "text/plain" or (not media_type and start not in (b"<", b"{")):
        try:
            message = body.decode("utf-8")
        except UnicodeError:
            return None
    if not isinstance(message, str):
        return None
    return SAFE_QUOTA_DIAGNOSTIC if _QUOTA_EXHAUSTED.search(message) else None


def http_date_to_iso(value: str | None) -> str | None:
    """RFC 1123 date (``getlastmodified``) to ISO 8601 UTC, e.g. ``2026-10-07T12:00:00Z``."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):  # e.g. year 9999 with a negative offset
        return None


def file_entry(item: DavResponse, path: str) -> dict[str, Any]:
    """Build a file tree entry from a PROPFIND response."""
    is_folder = item.has_child(tag(DAV, "resourcetype"), tag(DAV, "collection"))
    size_text = item.text(tag(DAV, "getcontentlength"))
    try:
        size = int(size_text) if size_text is not None else None
    except ValueError:
        size = None
    return {
        "path": path,
        "name": path.rsplit("/", 1)[-1] if path else "",
        "type": "folder" if is_folder else "file",
        "size": size,
        "modified": http_date_to_iso(item.text(tag(DAV, "getlastmodified"))),
        "etag": normalize_etag(item.text(tag(DAV, "getetag"))),
        "content_type": item.text(tag(DAV, "getcontenttype")),
    }
