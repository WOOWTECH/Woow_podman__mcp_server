"""The nine MCP tools and their registration (with READONLY / ALLOW_DELETE / DISABLED_TOOLS)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, NotRequired
from urllib.parse import quote, unquote, urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from fastmcp.utilities.types import NotSet as _NOT_SET
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict

from .caldav import calendar_entry, is_done, sort_tasks, tasks_from_report
from .client import BodyTooLarge, NextcloudClient, unprocessable
from .errors import (
    ETAG_MISMATCH,
    INVALID_RESPONSE,
    GatewayDenied,
    NextcloudHTTPError,
    Operation,
    coded,
    http_code,
    stale_message,
)
from .paths import (
    caller_etag,
    display_path,
    href_to_path,
    normalize_etag,
    normalize_path,
    quote_etag,
)
from .settings import Settings
from .webdav import (
    CALENDAR_PROPFIND,
    ETAG_PROPFIND,
    FILE_PROPFIND,
    TODO_REPORT,
    DavResponse,
    file_entry,
    parse_multistatus,
    tag,
)

logger = logging.getLogger("nextcloud_mcp_server")

XML_CONTENT_TYPE = "application/xml; charset=utf-8"
OFFLOAD_BYTES = 256 * 1024  # parse answers larger than this in a worker thread
# Nextcloud's ETags have about one-second resolution for writes to the same file: a
# second write within that second may keep the old ETag (or still match it).
UNCHANGED_ETAG_NOTE = (
    "Nextcloud did not change the etag; wait a second before the next conditional write "
    "to this file."
)
DELETE_NOTE = "Moved to the Nextcloud trash bin when the Deleted files app is enabled."
_MIME = mimetypes.MimeTypes()  # built-in table only: same answer on every platform

READ_TOOLS = (
    "get_file_tree",
    "get_file_content",
    "read_text_file",
    "list_calendars",
    "list_tasks",
)
WRITE_TOOLS = ("create_text_file", "update_text_file", "upload_file")
DELETE_TOOLS = ("delete_file_checked",)
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS + DELETE_TOOLS


# --------------------------------------------------------------------------- output types


class FileEntry(TypedDict):
    path: str
    name: str
    type: Literal["file", "folder"]
    size: int | None
    modified: str | None
    etag: str | None
    content_type: str | None


class FileTree(TypedDict):
    path: str
    entries: list[FileEntry]
    truncated: bool


class TextFile(TypedDict):
    path: str
    etag: str | None
    bytes: int
    content_type: str | None
    content: str


class CalendarInfo(TypedDict):
    id: str
    name: str
    components: list[str]
    color: str | None
    writable: bool


class CalendarList(TypedDict):
    calendars: list[CalendarInfo]


class TaskInfo(TypedDict):
    calendar: str
    uid: str | None
    summary: str | None
    status: str | None
    due: str | None
    start: str | None
    completed: str | None
    percent_complete: int | None
    priority: int | None
    description: str | None


class TaskList(TypedDict):
    tasks: list[TaskInfo]
    truncated: bool
    skipped_large_objects: int
    skipped_calendars: NotRequired[int]


class CreatedFile(TypedDict):
    status: Literal["created"]
    path: str
    etag: str | None
    bytes: int


class UpdatedFile(TypedDict):
    status: Literal["updated"]
    path: str
    previous_etag: str
    etag: str | None
    bytes: int
    note: NotRequired[str]


class UploadedFile(TypedDict):
    status: Literal["created", "replaced"]
    path: str
    etag: str | None
    bytes: int
    note: NotRequired[str]


class DeletedFile(TypedDict):
    status: Literal["deleted"]
    path: str
    etag: str
    note: str


# --------------------------------------------------------------------------- parameter types

PathArg = Annotated[
    str,
    Field(
        description=(
            "File path relative to the account's home folder, '/' as separator, e.g. "
            "'Documents/notes.md'. A leading '/' is optional. Do not percent-encode it."
        ),
        max_length=1024,
    ),
]
FolderArg = Annotated[
    str,
    Field(
        description=(
            "Folder (or file) path relative to the account's home folder; '' or '/' is the "
            "home folder itself. Do not percent-encode it."
        ),
        max_length=1024,
    ),
]
DepthArg = Annotated[
    int,
    Field(
        ge=1,
        le=3,
        description="How many folder levels to list: 1 = direct children only, at most 3.",
    ),
]
ContentArg = Annotated[
    str,
    Field(description="The complete new text of the file (UTF-8). It replaces nothing else."),
]
EtagArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=258,
        description=(
            "The etag returned by read_text_file or get_file_tree for the version you read. "
            "Quotes are optional."
        ),
    ),
]
OptionalEtagArg = Annotated[
    str | None,
    Field(
        max_length=258,
        description=(
            "Omit (or null) to create a new file only. To replace an existing file, give the "
            "etag of the version you read (from get_file_tree or read_text_file)."
        ),
    ),
]
Base64Arg = Annotated[
    str,
    Field(description="The file's bytes, standard base64 (RFC 4648, with padding)."),
]
CalendarArg = Annotated[
    str | None,
    Field(
        max_length=256,
        description=(
            "Calendar id from list_calendars to read only that calendar; omit (or null) to "
            "read every calendar that holds tasks."
        ),
    ),
]
IncludeCompletedArg = Annotated[
    bool,
    Field(description="Also return completed and cancelled tasks."),
]
LimitArg = Annotated[
    int,
    Field(ge=1, le=500, description="Maximum number of tasks to return (1-500)."),
]


# --------------------------------------------------------------------------- implementation


async def _offload(size: int, fn: Callable[..., Any], *args: Any) -> Any:
    """Run CPU-bound parsing of large answers off the event loop."""
    if size > OFFLOAD_BYTES:
        return await asyncio.to_thread(fn, *args)
    return fn(*args)


class NextcloudTools:
    """Tool implementations bound to one :class:`NextcloudClient`."""

    def __init__(self, nc: NextcloudClient, settings: Settings) -> None:
        self.nc = nc
        self.settings = settings

    async def run(self, call: Awaitable[Any]) -> Any:
        """Await a tool body and render its ToolError for MCP (code mode prefixes)."""
        try:
            return await call
        except ToolError as exc:
            raise self.nc.render(exc) from None
        except Exception as exc:  # CancelledError and other BaseException propagate
            raise self.nc.render(unprocessable(exc)) from None

    # -- helpers -------------------------------------------------------------------

    async def _propfind(
        self,
        url: str,
        depth: Literal["0", "1"],
        label: str,
        *,
        body: bytes = FILE_PROPFIND,
        op: Operation = "list",
    ) -> list[DavResponse]:
        try:
            reply = await self.nc.send(
                "PROPFIND",
                url,
                label=label,
                op=op,
                headers={"Depth": depth, "Content-Type": XML_CONTENT_TYPE},
                content=body,
                ok=(207,),
            )
        except BodyTooLarge:
            raise coded(
                f'The listing of "{label}" is too large to process.', INVALID_RESPONSE
            ) from None
        return await _offload(len(reply.body), parse_multistatus, reply.body)

    async def _entries(
        self, normalized: str, depth: Literal["0", "1"], *, op: Operation = "list"
    ) -> tuple[FileEntry | None, list[FileEntry]]:
        """PROPFIND a path; return (the resource itself, its direct children)."""
        home = await self.nc.files_home()
        home_path = urlsplit(home).path
        url = await self.nc.file_url(normalized)
        items = await self._propfind(url, depth, display_path(normalized), op=op)
        own: FileEntry | None = None
        children: list[FileEntry] = []
        prefix = f"{normalized}/" if normalized else ""
        matched = False
        for item in items:
            raw = href_to_path(item.href, home_path)
            if raw is None:
                continue
            matched = True
            try:
                # Server paths go through the same checks as user input; anything that is
                # not a safe user path (e.g. ending in "/..") is dropped, never followed.
                path = normalize_path(raw)
            except ToolError:
                logger.info("ignored a server path that is not a safe user path")
                continue
            entry: FileEntry = file_entry(item, path)  # type: ignore[assignment]
            if path == normalized:
                own = entry
            elif path.startswith(prefix) and "/" not in path[len(prefix) :]:
                children.append(entry)
        if items and not matched:
            if len(items) == 1:
                # A single response (always so for Depth 0, and for Depth 1 on a file or an
                # empty folder) describes the requested resource itself.
                own = file_entry(items[0], normalized)  # type: ignore[assignment]
            else:
                raise coded(
                    f"Nextcloud answered with paths outside {unquote(home_path)}; check "
                    "NEXTCLOUD_MCP_BASE_URL and, behind a reverse proxy, the 'overwritewebroot' "
                    "setting of Nextcloud.",
                    INVALID_RESPONSE,
                )
        return own, children

    async def _stat(self, normalized: str, *, op: Operation = "read") -> FileEntry:
        own, _ = await self._entries(normalized, "0", op=op)
        if own is None:
            raise coded(f'"{display_path(normalized)}" does not exist.', http_code(404))
        return own

    async def _current_state(self, url: str, label: str) -> tuple[bool, str | None]:
        """Best-effort PROPFIND: (exists, current etag). ``exists`` is True when unknown."""
        try:
            items = await self._propfind(url, "0", label, body=ETAG_PROPFIND, op="read")
        except NextcloudHTTPError as exc:
            if exc.status in (401, 429):
                raise  # authentication latch: report it, not a stale etag
            return exc.status != 404, None
        except ToolError:
            return True, None
        for item in items:
            etag = normalize_etag(item.text(tag("DAV:", "getetag")))
            if etag:
                return True, etag
        return True, None

    async def _precondition_failed(self, url: str, label: str, missing_hint: str) -> ToolError:
        """Explain a 412 on a guarded write: the file is gone, or it changed."""
        exists, etag = await self._current_state(url, label)
        if not exists:
            return coded(f'"{label}" does not exist; {missing_hint}.', http_code(404))
        return coded(stale_message(label, etag), http_code(412))

    async def _etag_after_write(self, url: str, label: str, headers: Any) -> str | None:
        etag = normalize_etag(headers.get("etag") or headers.get("oc-etag"))
        if etag:
            return etag
        return (await self._current_state(url, label))[1]

    @staticmethod
    def _file_path(path: str) -> str:
        normalized = normalize_path(path)
        if normalized == "":
            raise ToolError("path must name a file, not the home folder.")
        return normalized

    def _text_payload(self, content: str) -> bytes:
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            raise ToolError(
                "content is not valid UTF-8 text (it contains lone surrogates)."
            ) from None
        limit = self.settings.max_text_bytes
        if len(data) > limit:
            raise ToolError(
                f"content is {len(data)} bytes as UTF-8, more than the {limit}-byte limit "
                "(NEXTCLOUD_MCP_MAX_TEXT_BYTES)."
            )
        return data

    def _too_large(self, label: str, size: int | None = None) -> ToolError:
        limit = self.settings.max_text_bytes
        size_text = f"{size} bytes, " if size is not None else ""
        return coded(
            f'"{label}" is too large for the text tools ({size_text}limit {limit} bytes, '
            "NEXTCLOUD_MCP_MAX_TEXT_BYTES).",
            INVALID_RESPONSE,
        )

    # -- read tools ----------------------------------------------------------------

    async def get_file_tree(self, path: str = "", depth: int = 1) -> FileTree:
        normalized = normalize_path(path)
        limit = self.settings.tree_max_entries
        own, children = await self._entries(normalized, "1")
        if own is not None and own["type"] == "file":
            return {"path": normalized, "entries": [own], "truncated": False}

        def order(entries: list[FileEntry]) -> list[FileEntry]:
            return sorted(entries, key=lambda e: (e["type"] != "folder", e["name"].casefold()))

        listing: dict[str, list[FileEntry]] = {}
        count = 0
        truncated = False

        def add(parent: str, entries: list[FileEntry]) -> list[FileEntry]:
            nonlocal count, truncated
            entries = order(entries)
            room = limit - count
            if len(entries) > room:
                entries = entries[:room]
                truncated = True
            listing[parent] = entries
            count += len(entries)
            return [e for e in entries if e["type"] == "folder"]

        frontier = add(normalized, children)
        level = 1
        refused = False
        while level < depth and frontier and not truncated:
            next_frontier: list[FileEntry] = []
            for folder in frontier:
                if count >= limit:
                    truncated = True
                    break
                try:
                    _, sub = await self._entries(folder["path"], "1")
                except NextcloudHTTPError as exc:
                    if exc.status in (403, 404):
                        logger.info("skipped a sub-folder that vanished or is not readable")
                        continue
                    raise
                except GatewayDenied:
                    # e.g. a folder name with '%' that the gateway's policy refuses
                    logger.info("skipped a sub-folder the gateway refused")
                    refused = True
                    continue
                next_frontier.extend(add(folder["path"], sub))
                if truncated:
                    break
            frontier = next_frontier
            level += 1

        result: list[FileEntry] = []

        def emit(parent: str) -> None:
            for entry in listing.get(parent, []):
                result.append(entry)
                if entry["type"] == "folder":
                    emit(entry["path"])

        emit(normalized)
        return {"path": normalized, "entries": result, "truncated": truncated or refused}

    async def read_text_file(self, path: str) -> TextFile:
        normalized = normalize_path(path)
        if normalized == "":
            raise ToolError('"/" is the home folder; use get_file_tree to list it.')
        label = normalized
        entry = await self._stat(normalized)
        if entry["type"] == "folder":
            raise ToolError(f'"{label}" is a folder; use get_file_tree to list it.')
        limit = self.settings.max_text_bytes
        if entry["size"] is not None and entry["size"] > limit:
            raise self._too_large(label, entry["size"])
        url = await self.nc.file_url(normalized)
        try:
            reply = await self.nc.send(
                "GET", url, label=label, op="read", max_body=limit, ok=(200,)
            )
        except BodyTooLarge:
            raise self._too_large(label) from None
        data = reply.body
        not_text = ToolError(
            f'"{label}" is not a UTF-8 text file; use get_file_tree to see its type.'
        )
        if b"\x00" in data:
            raise not_text
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise not_text from None
        if text.startswith("﻿"):
            text = text[1:]
        header_type = reply.headers.get("content-type")
        return {
            "path": normalized,
            "etag": normalize_etag(reply.headers.get("etag")) or entry["etag"],
            "bytes": len(data),
            "content_type": entry["content_type"] or header_type,
            "content": text,
        }

    async def get_file_content(self, path: str) -> str:
        return (await self.read_text_file(path))["content"]

    async def _calendars(self) -> tuple[str, list[CalendarInfo]]:
        home = await self.nc.calendars_home()
        try:
            items = await self._propfind(
                home, "1", "calendars", body=CALENDAR_PROPFIND, op="calendar"
            )
        except NextcloudHTTPError as exc:
            if exc.status == 404:
                raise coded(
                    "This account has no calendar home; the calendar (CalDAV) service may be "
                    "disabled on this Nextcloud.",
                    http_code(404),
                ) from None
            raise
        calendars: list[CalendarInfo] = []
        for item in items:
            entry = calendar_entry(item)
            if entry is not None:
                calendars.append(entry)  # type: ignore[arg-type]
        calendars.sort(key=lambda c: (c["name"].casefold(), c["id"]))
        return home, calendars

    async def list_calendars(self) -> CalendarList:
        _, calendars = await self._calendars()
        return {"calendars": calendars}

    async def list_tasks(
        self,
        calendar: str | None = None,
        include_completed: bool = False,
        limit: int = 100,
    ) -> TaskList:
        home, calendars = await self._calendars()
        if calendar is not None:
            targets = [c for c in calendars if c["id"] == calendar]
            if not targets:
                raise ToolError(
                    f'Unknown calendar "{calendar}"; call list_calendars to see the ids.'
                )
            if "VTODO" not in targets[0]["components"]:
                raise ToolError(
                    f'Calendar "{calendar}" does not hold tasks (VTODO); pick one whose '
                    "components include VTODO."
                )
        else:
            targets = [c for c in calendars if "VTODO" in c["components"]]
        tasks: list[dict[str, Any]] = []
        skipped = 0
        skipped_calendars = 0
        for target in targets:
            url = f"{home}{quote(target['id'], safe='')}/"
            try:
                reply = await self.nc.send(
                    "REPORT",
                    url,
                    label=target["id"],
                    op="calendar",
                    headers={"Depth": "1", "Content-Type": XML_CONTENT_TYPE},
                    content=TODO_REPORT,
                    ok=(207,),
                )
            except BodyTooLarge:
                raise coded(
                    f'The tasks of calendar "{target["id"]}" are too large to process.',
                    INVALID_RESPONSE,
                ) from None
            except NextcloudHTTPError as exc:
                if calendar is None and exc.status in (403, 404):
                    logger.info("skipped a calendar that could not be read")
                    skipped_calendars += 1
                    continue
                raise
            except GatewayDenied:
                if calendar is None:
                    # e.g. a calendar id containing '%' that the gateway refuses
                    logger.info("skipped a calendar the gateway refused")
                    skipped_calendars += 1
                    continue
                raise
            found, too_large = await _offload(
                len(reply.body), tasks_from_report, target["id"], reply.body
            )
            tasks.extend(found)
            skipped += too_large
        if not include_completed:
            tasks = [t for t in tasks if not is_done(t)]
        ordered = sort_tasks(tasks)
        for task in ordered:
            task.pop("_due_sort", None)
        result: TaskList = {
            "tasks": ordered[:limit],  # type: ignore[typeddict-item]
            "truncated": len(ordered) > limit,
            "skipped_large_objects": skipped,
        }
        if skipped_calendars:
            result["skipped_calendars"] = skipped_calendars
        return result

    # -- write tools ---------------------------------------------------------------

    async def create_text_file(self, path: str, content: str) -> CreatedFile:
        normalized = self._file_path(path)
        data = self._text_payload(content)
        content_type = (
            "text/markdown; charset=utf-8"
            if normalized.lower().endswith(".md")
            else "text/plain; charset=utf-8"
        )
        url = await self.nc.file_url(normalized)
        reply = await self.nc.send(
            "PUT",
            url,
            label=normalized,
            op="create",
            headers={"If-None-Match": "*", "Content-Type": content_type},
            content=data,
        )
        etag = await self._etag_after_write(url, normalized, reply.headers)
        return {"status": "created", "path": normalized, "etag": etag, "bytes": len(data)}

    async def update_text_file(self, path: str, content: str, expected_etag: str) -> UpdatedFile:
        normalized = self._file_path(path)
        previous = caller_etag(expected_etag)
        data = self._text_payload(content)
        content_type = (
            "text/markdown; charset=utf-8"
            if normalized.lower().endswith(".md")
            else "text/plain; charset=utf-8"
        )
        url = await self.nc.file_url(normalized)
        try:
            reply = await self.nc.send(
                "PUT",
                url,
                label=normalized,
                op="update",
                headers={"If-Match": quote_etag(previous), "Content-Type": content_type},
                content=data,
            )
        except NextcloudHTTPError as exc:
            if exc.status == 412:
                raise await self._precondition_failed(
                    url, normalized, "use create_text_file to create it"
                ) from None
            raise
        etag = await self._etag_after_write(url, normalized, reply.headers)
        result: UpdatedFile = {
            "status": "updated",
            "path": normalized,
            "previous_etag": previous,
            "etag": etag,
            "bytes": len(data),
        }
        if etag == previous:
            result["note"] = UNCHANGED_ETAG_NOTE
        return result

    def _decode_upload(self, content_base64: str) -> bytes:
        cleaned = "".join(content_base64.split())
        limit = self.settings.max_upload_bytes
        if (len(cleaned) // 4) * 3 > limit + 2:
            raise ToolError(
                f"The decoded file would be larger than the {limit}-byte upload limit "
                "(NEXTCLOUD_MCP_MAX_UPLOAD_BYTES)."
            )
        try:
            data = base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError):
            raise ToolError(
                "content_base64 is not valid base64 (standard alphabet with '=' padding)."
            ) from None
        if len(data) > limit:
            raise ToolError(
                f"The decoded file is {len(data)} bytes, larger than the {limit}-byte upload "
                "limit (NEXTCLOUD_MCP_MAX_UPLOAD_BYTES)."
            )
        return data

    async def upload_file(
        self, path: str, content_base64: str, expected_etag: str | None = None
    ) -> UploadedFile:
        normalized = self._file_path(path)
        previous = None if expected_etag is None else caller_etag(expected_etag)
        data = self._decode_upload(content_base64)
        name = normalized.rsplit("/", 1)[-1]
        content_type = _MIME.guess_type(name, strict=False)[0] or "application/octet-stream"
        url = await self.nc.file_url(normalized)
        if previous is None:
            headers = {"If-None-Match": "*", "Content-Type": content_type}
            op: Operation = "upload_create"
        else:
            headers = {"If-Match": quote_etag(previous), "Content-Type": content_type}
            op = "upload_replace"
        try:
            reply = await self.nc.send(
                "PUT", url, label=normalized, op=op, headers=headers, content=data
            )
        except NextcloudHTTPError as exc:
            if exc.status == 412 and previous is not None:
                raise await self._precondition_failed(
                    url, normalized, "upload it without expected_etag to create it"
                ) from None
            raise
        etag = await self._etag_after_write(url, normalized, reply.headers)
        uploaded: UploadedFile = {
            "status": "created" if previous is None else "replaced",
            "path": normalized,
            "etag": etag,
            "bytes": len(data),
        }
        if previous is not None and etag == previous:
            uploaded["note"] = UNCHANGED_ETAG_NOTE
        return uploaded

    # -- delete tool ---------------------------------------------------------------

    async def delete_file_checked(self, path: str, expected_etag: str) -> DeletedFile:
        normalized = self._file_path(path)
        expected = caller_etag(expected_etag)
        entry = await self._stat(normalized, op="delete")
        if entry["type"] == "folder":
            raise ToolError(f'"{normalized}" is a folder; this tool deletes single files only.')
        if entry["etag"] and entry["etag"] != expected:
            # local pre-check: nothing was sent to delete the file
            raise coded(stale_message(normalized, entry["etag"]), ETAG_MISMATCH)
        url = await self.nc.file_url(normalized)
        try:
            await self.nc.send(
                "DELETE",
                url,
                label=normalized,
                op="delete",
                headers={"If-Match": quote_etag(expected)},
                ok=(200, 204),
            )
        except NextcloudHTTPError as exc:
            if exc.status == 412:
                raise await self._precondition_failed(
                    url, normalized, "nothing was deleted"
                ) from None
            raise
        return {"status": "deleted", "path": normalized, "etag": expected, "note": DELETE_NOTE}


# --------------------------------------------------------------------------- registration

DESCRIPTIONS: dict[str, str] = {
    "get_file_tree": (
        "List files and folders of the Nextcloud account, starting at a folder (default: the "
        "home folder) and going down up to `depth` levels (1-3). Each entry has path, name, "
        "type (file/folder), size, modified (UTC), etag and content_type; folders come first. "
        "The listing stops at the configured maximum and then sets truncated=true (also when "
        "a sub-folder could not be listed because the gateway refused it); list a "
        "sub-folder to see more. If `path` is a file, that file is the only entry. Next: "
        "read_text_file to read a text file; use the etag for updates or deletes."
    ),
    "get_file_content": (
        "Return the text of one UTF-8 text file as plain text, without metadata. Refuses "
        "folders, binary files and files over the size limit. Use read_text_file instead when "
        "you need the etag (for example before update_text_file)."
    ),
    "read_text_file": (
        "Read one UTF-8 text file. Returns path, etag, bytes, content_type and content (a "
        "leading byte-order mark is removed). Refuses folders, binary files and files over "
        "the size limit; use get_file_tree to see a file's type and size. Keep the etag: "
        "update_text_file and delete_file_checked need it."
    ),
    "list_calendars": (
        "List the calendars of the account with id, name, components (VEVENT, VTODO, ...), "
        "color and whether you may write to them. Pass an id to list_tasks to read its tasks."
    ),
    "list_tasks": (
        "List tasks (VTODO) from one calendar (`calendar` = id from list_calendars) or from "
        "all calendars that hold tasks. Open tasks only unless include_completed is true. "
        "Sorted open first, then by due date (tasks without due date last), then summary; "
        "at most `limit` tasks, truncated=true when more exist. Descriptions are cut at "
        "500 characters. Recurring tasks are not expanded: due/start are those of the first "
        "occurrence; objects holding only changed occurrences are skipped, and objects over "
        "1 MiB or nested deeper than 32 levels are counted in skipped_large_objects; calendars "
        "that could not be read (only when no calendar is given) are counted in "
        "skipped_calendars. Read-only: this server cannot change tasks."
    ),
    "create_text_file": (
        "Create a NEW text file with the given UTF-8 content. Never overwrites: if the file "
        "exists the call fails, and you should read_text_file it and use update_text_file with "
        "its etag. The parent folder must already exist (this server cannot create folders). "
        "Returns status, path, etag and bytes."
    ),
    "update_text_file": (
        "Replace the whole content of an existing text file, but only if it is still the "
        "version you read: pass the etag from read_text_file as expected_etag. If someone "
        "changed the file meanwhile the call fails with the current etag; read it again, "
        "merge, and retry only then. Returns status, path, previous_etag, etag and bytes."
    ),
    "upload_file": (
        "Upload a file (any type) from base64 content. Without expected_etag it only creates "
        "a new file and fails if one exists; with expected_etag it replaces exactly that "
        "version. The parent folder must exist. Size is limited by the server settings. "
        "Returns status (created/replaced), path, etag and bytes."
    ),
    "delete_file_checked": (
        "Delete one file (never a folder), only if it is still the version you read: pass "
        "its etag as expected_etag. The file goes to the Nextcloud trash bin when the Deleted "
        "files app is enabled. If the file changed the call fails; read it again and ask the "
        "user before retrying. Returns status, path, the deleted etag and a note."
    ),
}

TITLES = {
    "get_file_tree": "List files and folders",
    "get_file_content": "Get file text",
    "read_text_file": "Read text file",
    "list_calendars": "List calendars",
    "list_tasks": "List tasks",
    "create_text_file": "Create text file",
    "update_text_file": "Update text file",
    "upload_file": "Upload file",
    "delete_file_checked": "Delete file (etag checked)",
}


def _annotations(name: str) -> ToolAnnotations:
    if name in READ_TOOLS:
        return ToolAnnotations(
            title=TITLES[name], readOnlyHint=True, destructiveHint=False, idempotentHint=True
        )
    if name in WRITE_TOOLS:
        # Every write is guarded by If-None-Match / If-Match, so repeating a call cannot
        # change anything a second time. Replacing content is destructive (even though
        # Nextcloud usually keeps versions); only create_text_file never overwrites.
        return ToolAnnotations(
            title=TITLES[name],
            readOnlyHint=False,
            destructiveHint=name != "create_text_file",
            idempotentHint=True,
        )
    return ToolAnnotations(
        title=TITLES[name], readOnlyHint=False, destructiveHint=True, idempotentHint=True
    )


def _functions(impl: NextcloudTools) -> dict[str, Callable[..., Any]]:
    async def get_file_tree(path: FolderArg = "", depth: DepthArg = 1) -> FileTree:
        return await impl.run(impl.get_file_tree(path, depth))

    async def get_file_content(path: PathArg) -> str:
        return await impl.run(impl.get_file_content(path))

    async def read_text_file(path: PathArg) -> TextFile:
        return await impl.run(impl.read_text_file(path))

    async def list_calendars() -> CalendarList:
        return await impl.run(impl.list_calendars())

    async def list_tasks(
        calendar: CalendarArg = None,
        include_completed: IncludeCompletedArg = False,
        limit: LimitArg = 100,
    ) -> TaskList:
        return await impl.run(impl.list_tasks(calendar, include_completed, limit))

    async def create_text_file(path: PathArg, content: ContentArg) -> CreatedFile:
        return await impl.run(impl.create_text_file(path, content))

    async def update_text_file(
        path: PathArg, content: ContentArg, expected_etag: EtagArg
    ) -> UpdatedFile:
        return await impl.run(impl.update_text_file(path, content, expected_etag))

    async def upload_file(
        path: PathArg, content_base64: Base64Arg, expected_etag: OptionalEtagArg = None
    ) -> UploadedFile:
        return await impl.run(impl.upload_file(path, content_base64, expected_etag))

    async def delete_file_checked(path: PathArg, expected_etag: EtagArg) -> DeletedFile:
        return await impl.run(impl.delete_file_checked(path, expected_etag))

    return {
        "get_file_tree": get_file_tree,
        "get_file_content": get_file_content,
        "read_text_file": read_text_file,
        "list_calendars": list_calendars,
        "list_tasks": list_tasks,
        "create_text_file": create_text_file,
        "update_text_file": update_text_file,
        "upload_file": upload_file,
        "delete_file_checked": delete_file_checked,
    }


def enabled_tools(settings: Settings) -> list[str]:
    """Names of the tools this configuration registers, in a stable order."""
    names = list(READ_TOOLS)
    if not settings.readonly:
        names += WRITE_TOOLS
        if settings.allow_delete:
            names += DELETE_TOOLS
    disabled = settings.disabled_tool_names  # unknown names are rejected by Settings
    return [name for name in names if name not in disabled]


def build_tool(name: str, fn: Callable[..., Any]) -> Tool:
    """Create the FastMCP tool with a closed input schema (``additionalProperties: false``)."""
    tool = Tool.from_function(
        fn,
        name=name,
        description=DESCRIPTIONS[name],
        annotations=_annotations(name),
        # get_file_content answers with plain text only (no structured content).
        output_schema=None if name == "get_file_content" else _NOT_SET,
    )
    parameters = dict(tool.parameters)
    parameters["type"] = "object"
    parameters.setdefault("properties", {})
    parameters["additionalProperties"] = False
    tool.parameters = parameters
    return tool


def register_tools(server: FastMCP, nc: NextcloudClient, settings: Settings) -> list[str]:
    """Register the enabled tools on ``server``; return their names."""
    impl = NextcloudTools(nc, settings)
    functions = _functions(impl)
    names = enabled_tools(settings)
    for name in names:
        server.add_tool(build_tool(name, functions[name]))
    return names
