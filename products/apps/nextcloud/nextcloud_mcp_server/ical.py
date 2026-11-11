"""A small, tolerant reader for the parts of iCalendar (RFC 5545) that ``list_tasks`` needs.

Only reading is supported: line unfolding (§3.1), content lines with parameters
(§3.1, §3.2), TEXT unescaping (§3.3.11), DATE / DATE-TIME values (§3.3.4, §3.3.5)
and INTEGER values. Broken lines or values are skipped instead of failing the
whole object, because calendars are written by many different clients.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_NAME = re.compile(r"[A-Za-z0-9-]+")
_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})")
_DATE_TIME = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)")


@dataclass(frozen=True)
class Property:
    name: str
    params: dict[str, str]
    value: str


_FOLD = re.compile(r"\n[ \t]")

# Windows / Outlook time zone names seen in TZID parameters, mapped to IANA names.
# Source: the public Unicode CLDR "windowsZones" table (territory 001 entries).
# Names not listed here (and not IANA names) keep the time floating.
WINDOWS_ZONES = {
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "E. South America Standard Time": "America/Sao_Paulo",
    "India Standard Time": "Asia/Calcutta",
    "China Standard Time": "Asia/Shanghai",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "Singapore Standard Time": "Asia/Singapore",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Arabian Standard Time": "Asia/Dubai",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
}


def unfold(text: str) -> list[str]:
    """Split into logical lines, joining folded continuation lines (CRLF/LF + space or tab).

    Linear in the input size (no repeated string concatenation).
    """
    text = _FOLD.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return [line for line in text.split("\n") if line and line[0] not in " \t"]


def _zone(tzid: str) -> ZoneInfo | None:
    """IANA zone for a TZID: IANA name, known Windows name, or a prefixed IANA path."""
    name = tzid.strip().strip('"')
    candidates = [WINDOWS_ZONES.get(name, name)]
    parts = [p for p in name.split("/") if p]
    if len(parts) > 2:  # e.g. "/mozilla.org/20050126_1/Europe/Berlin"
        candidates += ["/".join(parts[-2:]), "/".join(parts[-3:])]
    elif name.startswith("/"):
        candidates.append("/".join(parts))
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return None


def _split_outside_quotes(text: str, separator: str, maxsplit: int = -1) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == separator and not quoted and (maxsplit < 0 or len(parts) < maxsplit):
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def parse_line(line: str) -> Property | None:
    """Parse ``NAME;PARAM=value:VALUE``; returns ``None`` for lines that cannot be read."""
    quoted = False
    colon = -1
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            colon = index
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1 :]
    pieces = _split_outside_quotes(head, ";")
    name = pieces[0].strip()
    if not _NAME.fullmatch(name):
        return None
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        key, sep, raw = piece.partition("=")
        if not sep or not _NAME.fullmatch(key.strip()):
            return None
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            raw = raw[1:-1]
        params[key.strip().upper()] = raw
    return Property(name=name.upper(), params=params, value=value)


def unescape_text(value: str) -> str:
    """Undo TEXT escaping: ``\\n``/``\\N`` newline, ``\\\\``, ``\\;``, ``\\,``."""
    out: list[str] = []
    chars = iter(value)
    for char in chars:
        if char != "\\":
            out.append(char)
            continue
        following = next(chars, "")
        if following in ("n", "N"):
            out.append("\n")
        elif following in ("\\", ";", ",", '"', ":"):
            out.append(following)
        else:
            out.append("\\" + following)
    return "".join(out)


def parse_date_value(prop: Property) -> date | datetime | None:
    """DATE or DATE-TIME (UTC ``Z``, ``TZID`` or floating); ``None`` when unreadable."""
    value = prop.value.strip()
    try:
        if prop.params.get("VALUE", "").upper() == "DATE" or _DATE.fullmatch(value):
            match = _DATE.fullmatch(value)
            if not match:
                return None
            return date(int(match[1]), int(match[2]), int(match[3]))
        match = _DATE_TIME.fullmatch(value)
        if not match:
            return None
        moment = datetime(*(int(match[i]) for i in range(1, 7)))
    except ValueError:
        return None
    if match[7]:
        return moment.replace(tzinfo=UTC)
    tzid = prop.params.get("TZID")
    if tzid:
        zone = _zone(tzid)
        if zone is not None:
            return moment.replace(tzinfo=zone)
        # unknown zone name (e.g. "(UTC+08:00) Taipei"): keep the time floating
    return moment


def parse_int(prop: Property | None) -> int | None:
    if prop is None:
        return None
    try:
        return int(prop.value.strip())
    except ValueError:
        return None


MAX_NESTING = 32


class ObjectTooDeep(ValueError):
    """Components nested deeper than :data:`MAX_NESTING`; the object is skipped."""


def components(text: str, wanted: str) -> list[dict[str, Property]]:
    """Properties (first occurrence of each name) of every ``wanted`` component.

    Nested sub-components (for example a VALARM inside a VTODO) do not contribute
    their properties. Components that are never closed are dropped. Raises
    :class:`ObjectTooDeep` when components nest deeper than :data:`MAX_NESTING`.
    Linear in the input: open component names are tracked with a counter, so an END
    for a name that is not open is detected in O(1).
    """
    wanted = wanted.upper()
    found: list[dict[str, Property]] = []
    stack: list[str] = []
    open_names: Counter[str] = Counter()
    current: dict[str, Property] | None = None
    current_depth = -1
    for line in unfold(text):
        prop = parse_line(line)
        if prop is None:
            continue
        if prop.name == "BEGIN":
            name = prop.value.strip().upper()
            if len(stack) >= MAX_NESTING:
                raise ObjectTooDeep(f"components nested deeper than {MAX_NESTING}")
            stack.append(name)
            open_names[name] += 1
            if name == wanted and current is None:
                current = {}
                current_depth = len(stack)
            continue
        if prop.name == "END":
            name = prop.value.strip().upper()
            if open_names[name] > 0:
                while stack:
                    closed = stack.pop()
                    open_names[closed] -= 1
                    if current is not None and len(stack) < current_depth:
                        if closed == wanted and name == wanted:
                            found.append(current)
                        current = None
                        current_depth = -1
                    if closed == name:
                        break
            continue
        if current is not None and len(stack) == current_depth:
            current.setdefault(prop.name, prop)
    return found
