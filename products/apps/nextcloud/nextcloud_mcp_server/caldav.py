"""Calendar collection and VTODO parsing (RFC 4791 / RFC 5545)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from .ical import ObjectTooDeep, Property, components, parse_date_value, parse_int, unescape_text
from .webdav import APPLE_ICAL, CALDAV, DAV, DavResponse, parse_multistatus, tag

ALL_COMPONENTS = ["VEVENT", "VJOURNAL", "VTODO"]
DONE_STATUSES = frozenset({"COMPLETED", "CANCELLED"})
DESCRIPTION_LIMIT = 500
MAX_OBJECT_CHARS = 1024 * 1024  # calendar objects larger than this are skipped


def href_last_segment(href: str) -> str:
    """Decoded last non-empty path segment of an href."""
    try:
        path = urlsplit(href).path.rstrip("/")
    except ValueError:  # unparsable href: no id, so the collection is skipped
        return ""
    return unquote(path.rsplit("/", 1)[-1])


def _privileges(item: DavResponse) -> set[str]:
    element = item.props.get(tag(DAV, "current-user-privilege-set"))
    if element is None:
        return set()
    names: set[str] = set()
    for privilege in element.findall(tag(DAV, "privilege")):
        for child in privilege:
            names.add(child.tag)
    return names


def calendar_entry(item: DavResponse) -> dict[str, Any] | None:
    """A calendar for ``list_calendars`` or ``None`` when the collection is no calendar."""
    if not item.has_child(tag(DAV, "resourcetype"), tag(CALDAV, "calendar")):
        return None
    calendar_id = href_last_segment(item.href)
    if not calendar_id:
        return None
    component_set = item.props.get(tag(CALDAV, "supported-calendar-component-set"))
    if component_set is None:
        # RFC 4791 §5.2.3: without the property the server accepts every component type.
        components = list(ALL_COMPONENTS)
    else:
        components = sorted(
            {
                (comp.get("name") or "").upper()
                for comp in component_set.findall(tag(CALDAV, "comp"))
                if comp.get("name")
            }
        )
    privileges = _privileges(item)
    writable = bool(privileges & {tag(DAV, "write"), tag(DAV, "write-content"), tag(DAV, "all")})
    return {
        "id": calendar_id,
        "name": item.text(tag(DAV, "displayname")) or calendar_id,
        "components": components,
        "color": item.text(tag(APPLE_ICAL, "calendar-color")),
        "writable": writable,
    }


def _iso(moment: date | datetime | None) -> str | None:
    if moment is None:
        return None
    if isinstance(moment, datetime):
        if moment.tzinfo is UTC:
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        return moment.isoformat()
    return moment.isoformat()


def _sort_moment(moment: date | datetime | None) -> float | None:
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp()
    if isinstance(moment, date):
        return datetime(moment.year, moment.month, moment.day, tzinfo=UTC).timestamp()
    return None


def _text(props: dict[str, Property], name: str) -> str | None:
    prop = props.get(name)
    if prop is None:
        return None
    text = unescape_text(prop.value).strip()
    return text or None


def _date(props: dict[str, Property], name: str) -> date | datetime | None:
    prop = props.get(name)
    return parse_date_value(prop) if prop is not None else None


def parse_tasks(calendar_id: str, ics: str) -> list[dict[str, Any]]:
    """Tasks (VTODO) of one calendar object resource; unreadable parts are skipped.

    Recurrence rules are not expanded: a recurring task is listed once with the dates of
    its master component (the first occurrence). Objects that contain only overridden
    instances (RECURRENCE-ID) and no master yield no task.
    """
    tasks: list[dict[str, Any]] = []
    for props in components(ics, "VTODO"):
        if "RECURRENCE-ID" in props:
            # Overridden instances of a recurring task; the master component is listed.
            continue
        status = _text(props, "STATUS")
        description = _text(props, "DESCRIPTION")
        if description and len(description) > DESCRIPTION_LIMIT:
            description = description[: DESCRIPTION_LIMIT - 1] + "\u2026"
        due = _date(props, "DUE")
        tasks.append(
            {
                "calendar": calendar_id,
                "uid": _text(props, "UID"),
                "summary": _text(props, "SUMMARY"),
                "status": status.upper() if status else None,
                "due": _iso(due),
                "start": _iso(_date(props, "DTSTART")),
                "completed": _iso(_date(props, "COMPLETED")),
                "percent_complete": parse_int(props.get("PERCENT-COMPLETE")),
                "priority": parse_int(props.get("PRIORITY")),
                "description": description,
                "_due_sort": _sort_moment(due),
            }
        )
    return tasks


def tasks_from_report(calendar_id: str, body: bytes) -> tuple[list[dict[str, Any]], int]:
    """Tasks of a ``calendar-query`` REPORT answer and the number of skipped objects.

    Objects are skipped when larger than :data:`MAX_OBJECT_CHARS` or nested deeper than
    :data:`~nextcloud_mcp_server.ical.MAX_NESTING`.

    CPU-bound; callers run it in a worker thread for large answers.
    """
    tasks: list[dict[str, Any]] = []
    skipped = 0
    for item in parse_multistatus(body):
        data = item.props.get(tag(CALDAV, "calendar-data"))
        text = data.text if data is not None else None
        if not text or not text.strip():
            continue
        if len(text) > MAX_OBJECT_CHARS:
            skipped += 1
            continue
        try:
            tasks.extend(parse_tasks(calendar_id, text))
        except ObjectTooDeep:
            skipped += 1  # nested deeper than MAX_NESTING: skipped as a whole
    return tasks, skipped


def is_done(task: dict[str, Any]) -> bool:
    """Completed or cancelled (a COMPLETED date without STATUS also counts as done)."""
    if task["status"] in DONE_STATUSES:
        return True
    return task["status"] is None and task["completed"] is not None


def sort_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open first, then due ascending (no due last), then summary."""

    def key(task: dict[str, Any]) -> tuple[Any, ...]:
        due = task.get("_due_sort")
        return (
            is_done(task),
            due is None,
            due if due is not None else 0.0,
            (task["summary"] or "").casefold(),
            task["uid"] or "",
        )

    return sorted(tasks, key=key)
