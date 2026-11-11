# 複製自 WOOWTECH/Woow_nextcloud_mcp_server@b9b2a33 tests/fake_nextcloud.py（MIT，Copyright (c) 2026 WOOWTECH），未修改。
# 依公開 RFC 4918／4791 撰寫的記憶體內 Nextcloud 替身，供 httpx.MockTransport 使用（不連網）。
"""A small in-memory Nextcloud stand-in for ``httpx.MockTransport`` (no network).

It implements just enough of OCS, WebDAV and CalDAV for the tests, following the
public RFC 4918 / RFC 4791 behaviour (conditional requests, multistatus answers).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

import httpx

BASE_URL = "https://cloud.example.com/nc"
LOGIN = "alice@example.com"
USER_ID = "alice smith"
PASSWORD = "Sup3r-Secret-App-Pass"
LAST_MODIFIED = "Tue, 06 Oct 2026 10:20:30 GMT"


@dataclass
class FakeFile:
    data: bytes
    etag: str
    content_type: str = "text/plain"


@dataclass
class FakeCalendar:
    name: str
    components: list[str]
    color: str | None = "#0082c9"
    writable: bool = True
    objects: dict[str, str] = field(default_factory=dict)


Override = Callable[[httpx.Request], httpx.Response | None]


class FakeNextcloud:
    def __init__(self, user_id: str = USER_ID) -> None:
        self.user_id = user_id
        self.files: dict[str, FakeFile] = {}
        self.folders: set[str] = {""}
        self.calendars: dict[str, FakeCalendar] = {}
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.override: Override | None = None
        self.send_put_etag = True
        # Apache mod_deflate（DeflateAlterETag AddSuffix）對較大文字檔的 GET 回 ETag "<etag>-gzip"，PROPFIND 與寫入回應仍是原本的：
        # 內容超過這個大小的檔案，GET 的 ETag 加上後綴。
        self.gzip_etag_over: int | None = None
        self._etags = itertools.count(1)
        self.user_requests = 0

    # -- fixtures --------------------------------------------------------------------

    def new_etag(self) -> str:
        return f"etag{next(self._etags)}"

    def add_folder(self, path: str) -> None:
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            self.folders.add("/".join(parts[:i]))

    def add_file(self, path: str, data: bytes, content_type: str = "text/plain") -> FakeFile:
        if "/" in path:
            self.add_folder(path.rsplit("/", 1)[0])
        item = FakeFile(data=data, etag=self.new_etag(), content_type=content_type)
        self.files[path] = item
        return item

    def add_calendar(self, cal_id: str, name: str, components: list[str], **kw) -> FakeCalendar:
        cal = FakeCalendar(name=name, components=components, **kw)
        self.calendars[cal_id] = cal
        return cal

    # -- transport -------------------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def files_prefix(self) -> str:
        return f"/nc/remote.php/dav/files/{quote(self.user_id, safe='')}/"

    @property
    def calendars_prefix(self) -> str:
        return f"/nc/remote.php/dav/calendars/{quote(self.user_id, safe='')}/"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = request.read()
        self.bodies.append(body)
        raw_path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        if raw_path == "/nc/ocs/v2.php/cloud/user":
            self.user_requests += 1
            return httpx.Response(
                200, json={"ocs": {"meta": {"status": "ok"}, "data": {"id": self.user_id}}}
            )
        if self.override is not None:  # overrides apply to DAV requests only
            answer = self.override(request)
            if answer is not None:
                return answer
        if raw_path.startswith(self.files_prefix) or raw_path + "/" == self.files_prefix:
            rel = unquote(raw_path[len(self.files_prefix) :]).strip("/")
            return self._files(request, rel, body)
        if raw_path.startswith(self.calendars_prefix) or raw_path + "/" == self.calendars_prefix:
            rel = unquote(raw_path[len(self.calendars_prefix) :]).strip("/")
            return self._calendars(request, rel)
        return httpx.Response(404)

    # -- files -----------------------------------------------------------------------

    def _href(self, path: str, folder: bool) -> str:
        encoded = "/".join(quote(seg, safe="") for seg in path.split("/")) if path else ""
        href = self.files_prefix + encoded
        if folder and path:
            href += "/"
        return href

    def _file_response(self, path: str) -> str:
        if path in self.folders:
            return (
                f"<d:response><d:href>{escape(self._href(path, True))}</d:href>"
                "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>"
                f"<d:getlastmodified>{LAST_MODIFIED}</d:getlastmodified>"
                f'<d:getetag>"folder-{len(path)}"</d:getetag></d:prop>'
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
                "<d:propstat><d:prop><d:getcontentlength/><d:getcontenttype/></d:prop>"
                "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>"
            )
        item = self.files[path]
        return (
            f"<d:response><d:href>{escape(self._href(path, False))}</d:href>"
            "<d:propstat><d:prop><d:resourcetype/>"
            f"<d:getcontentlength>{len(item.data)}</d:getcontentlength>"
            f"<d:getlastmodified>{LAST_MODIFIED}</d:getlastmodified>"
            f'<d:getetag>"{escape(item.etag)}"</d:getetag>'
            f"<d:getcontenttype>{escape(item.content_type)}</d:getcontenttype></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    @staticmethod
    def _multistatus(inner: str, extra_ns: str = "") -> httpx.Response:
        xml = (
            '<?xml version="1.0"?>'
            f'<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns"{extra_ns}>'
            f"{inner}</d:multistatus>"
        )
        return httpx.Response(
            207, content=xml.encode(), headers={"Content-Type": "application/xml; charset=utf-8"}
        )

    def _files(self, request: httpx.Request, rel: str, body: bytes) -> httpx.Response:
        method = request.method
        exists_file = rel in self.files
        exists_folder = rel in self.folders
        if method == "PROPFIND":
            if not (exists_file or exists_folder):
                return httpx.Response(404)
            parts = [self._file_response(rel)]
            if exists_folder and request.headers.get("Depth") == "1":
                prefix = f"{rel}/" if rel else ""
                for child in sorted(self.folders | set(self.files)):
                    if child and child.startswith(prefix) and "/" not in child[len(prefix) :]:
                        parts.append(self._file_response(child))
            return self._multistatus("".join(parts))
        if method == "GET":
            if exists_folder:
                return httpx.Response(405)
            if not exists_file:
                return httpx.Response(404)
            item = self.files[rel]
            etag = item.etag
            if self.gzip_etag_over is not None and len(item.data) > self.gzip_etag_over:
                etag += "-gzip"
            return httpx.Response(
                200,
                content=item.data,
                headers={"ETag": f'"{etag}"', "Content-Type": item.content_type},
            )
        if method == "PUT":
            if exists_folder:
                return httpx.Response(405)
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if parent not in self.folders:
                return httpx.Response(404)  # what Nextcloud 35 answers (not 409)
            if request.headers.get("If-None-Match") == "*" and exists_file:
                return httpx.Response(412)
            if_match = request.headers.get("If-Match")
            if if_match is not None and (
                not exists_file or if_match != f'"{self.files[rel].etag}"'
            ):
                return httpx.Response(412)
            ctype = request.headers.get("Content-Type", "application/octet-stream")
            item = FakeFile(data=body, etag=self.new_etag(), content_type=ctype.split(";")[0])
            self.files[rel] = item
            headers = {"ETag": f'"{item.etag}"', "OC-ETag": f'"{item.etag}"'}
            if not self.send_put_etag:
                headers = {}
            return httpx.Response(204 if exists_file else 201, headers=headers)
        if method == "DELETE":
            if not exists_file:
                return httpx.Response(404)
            if_match = request.headers.get("If-Match")
            if if_match is not None and if_match != f'"{self.files[rel].etag}"':
                return httpx.Response(412)
            del self.files[rel]
            return httpx.Response(204)
        return httpx.Response(405)

    # -- calendars -------------------------------------------------------------------

    def _calendars(self, request: httpx.Request, rel: str) -> httpx.Response:
        ns = (
            ' xmlns:cal="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/"'
            ' xmlns:x1="http://apple.com/ns/ical/" xmlns:oc="http://owncloud.org/ns"'
            ' xmlns:nc="http://nextcloud.com/ns"'
        )
        if request.method == "PROPFIND" and rel == "":
            home = self.calendars_prefix
            parts = [
                f"<d:response><d:href>{home}</d:href><d:propstat><d:prop><d:resourcetype>"
                "<d:collection/></d:resourcetype></d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
                f"<d:response><d:href>{home}inbox/</d:href><d:propstat><d:prop>"
                "<d:resourcetype><d:collection/><cal:schedule-inbox/></d:resourcetype></d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
                f"<d:response><d:href>{home}outbox/</d:href><d:propstat><d:prop>"
                "<d:resourcetype><d:collection/><cal:schedule-outbox/></d:resourcetype></d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
                f"<d:response><d:href>{home}trashbin/</d:href><d:propstat><d:prop>"
                "<d:resourcetype><d:collection/><nc:trash-bin/></d:resourcetype></d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
                f"<d:response><d:href>{home}holidays-sub/</d:href><d:propstat><d:prop>"
                "<d:displayname>Holidays</d:displayname>"
                "<d:resourcetype><d:collection/><cs:subscribed/></d:resourcetype></d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
            ]
            for cal_id, cal in self.calendars.items():
                comps = "".join(f'<cal:comp name="{c}"/>' for c in cal.components)
                privs = "<d:privilege><d:read/></d:privilege>"
                if cal.writable:
                    privs += "<d:privilege><d:write/></d:privilege>"
                color = f"<x1:calendar-color>{cal.color}</x1:calendar-color>" if cal.color else ""
                missing = "" if cal.color else "<x1:calendar-color/>"
                parts.append(
                    f"<d:response><d:href>{home}{quote(cal_id, safe='')}/</d:href>"
                    f"<d:propstat><d:prop><d:displayname>{escape(cal.name)}</d:displayname>"
                    "<d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>"
                    f"<cal:supported-calendar-component-set>{comps}"
                    "</cal:supported-calendar-component-set>"
                    f"{color}<d:current-user-privilege-set>{privs}"
                    "</d:current-user-privilege-set><cs:getctag>ctag1</cs:getctag></d:prop>"
                    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
                    f"<d:propstat><d:prop>{missing}</d:prop>"
                    "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>"
                )
            return self._multistatus("".join(parts), ns)
        if request.method == "REPORT":
            cal = self.calendars.get(rel)
            if cal is None:
                return httpx.Response(404)
            parts = []
            for name, ics in cal.objects.items():
                href = f"{self.calendars_prefix}{quote(rel, safe='')}/{quote(name, safe='')}"
                parts.append(
                    f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                    f'<d:getetag>"{name}"</d:getetag>'
                    f"<cal:calendar-data>{escape(ics)}</cal:calendar-data></d:prop>"
                    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                )
            return self._multistatus("".join(parts), ns)
        return httpx.Response(405)


def vtodo(uid: str, summary: str, *lines: str) -> str:
    """A VCALENDAR with one VTODO; ``lines`` are extra raw content lines."""
    content = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//WOOW//tests//EN",
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        *lines,
        "END:VTODO",
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(content)
