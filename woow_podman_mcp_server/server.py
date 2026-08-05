#!/usr/bin/env python3
"""
podman-mcp-server — 把 Podman REST API (libpod) 包成 MCP server 給 AI 調用。

Design notes
------------
* 直接打 libpod REST API (httpx)，不 shell-out podman CLI：少一層 process、
  錯誤結構化 (ErrorModel {cause,message,response})、支援 unix / tcp / mTLS。
* 安全採 profile 制。工具在「啟動時」就依 profile 決定要不要註冊，
  沒註冊的工具在 protocol 層根本不存在 —— 避免 list-time-only 過濾造成的
  read-only bypass (參考 CVE-2026-46519 類型問題)。每個 mutating handler
  內部再 assert 一次 profile，雙保險。
* 輸出對 LLM 友善：預設 compact text table，format="json" 才給原始 JSON；
  所有輸出都有 char cap 與明確的 [truncated] 標記。
* logs / exec 走 libpod 的 8-byte multiplexed framing，一律 demux
  （注意：libpod 路徑即使 tty=true 也是 multiplexed，只有 /v1.x compat
   路徑在 tty 時才是 raw；很多從 docker 抄來的 client 在這裡會吃到亂碼）。

Env config
----------
  PODMAN_URI            unix:///run/user/1000/podman/podman.sock (default: auto)
                        tcp://host:8443 | https://host:8443
  PODMAN_API_VERSION    default v5.0.0  (libpod 路徑一定要版本前綴)
  PODMAN_MCP_PROFILE    readonly | safe | full        (default: safe)
  PODMAN_MCP_NAME_ALLOW regex，限制可操作的 container/pod 名稱 (default: 全部)
  PODMAN_MCP_MAX_CHARS  單次回應字元上限 (default: 20000)
  PODMAN_MCP_TIMEOUT    HTTP timeout 秒 (default: 60)
  PODMAN_TLS_CA / PODMAN_TLS_CERT / PODMAN_TLS_KEY   mTLS (tcp/https 才用)

Profiles
--------
  readonly  只讀：info/ps/inspect/logs/stats/top/images/pods/volumes/networks/
            df/events/healthcheck
  safe      readonly + start/stop/restart/exec/pull   (不含刪除)
  full      safe + run/kill/rm/rmi/prune             (含破壞性操作)

Run
---
  stdio:  python podman_mcp_server.py
  http :  python podman_mcp_server.py --http --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------- config ----

PROFILE = os.getenv("PODMAN_MCP_PROFILE", "safe").strip().lower()
if PROFILE not in ("readonly", "safe", "full"):
    sys.exit(f"PODMAN_MCP_PROFILE must be readonly|safe|full, got {PROFILE!r}")

API_VERSION = os.getenv("PODMAN_API_VERSION", "v5.0.0").strip().lstrip("/")
MAX_CHARS = int(os.getenv("PODMAN_MCP_MAX_CHARS", "20000"))
TIMEOUT = float(os.getenv("PODMAN_MCP_TIMEOUT", "60"))
NAME_ALLOW = os.getenv("PODMAN_MCP_NAME_ALLOW", "").strip()
_NAME_RE = re.compile(NAME_ALLOW) if NAME_ALLOW else None

_LEVEL = {"readonly": 0, "safe": 1, "full": 2}[PROFILE]


def _default_uri() -> str:
    for cand in (
        os.getenv("CONTAINER_HOST"),
        os.getenv("DOCKER_HOST"),
        f"unix://{os.getenv('XDG_RUNTIME_DIR', '/run/user/1000')}/podman/podman.sock",
        "unix:///run/podman/podman.sock",
    ):
        if not cand:
            continue
        if cand.startswith("unix://"):
            if os.path.exists(cand[len("unix://"):]):
                return cand
        else:
            return cand
    return "unix:///run/podman/podman.sock"


PODMAN_URI = os.getenv("PODMAN_URI") or _default_uri()


def _build_client() -> httpx.AsyncClient:
    """unix:// -> UDS transport；tcp:// / https:// -> 一般 HTTP(S)（可帶 mTLS）。"""
    common = dict(timeout=httpx.Timeout(TIMEOUT, read=TIMEOUT, connect=10.0))
    if PODMAN_URI.startswith("unix://"):
        sock = PODMAN_URI[len("unix://"):]
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=sock),
            base_url="http://d",
            **common,
        )
    base = PODMAN_URI
    if base.startswith("tcp://"):
        scheme = "https" if os.getenv("PODMAN_TLS_CERT") else "http"
        base = f"{scheme}://" + base[len("tcp://"):]
    ca = os.getenv("PODMAN_TLS_CA")
    cert = os.getenv("PODMAN_TLS_CERT")
    key = os.getenv("PODMAN_TLS_KEY")
    kw: dict[str, Any] = {}
    if ca:
        kw["verify"] = ca
    if cert and key:
        kw["cert"] = (cert, key)
    return httpx.AsyncClient(base_url=base.rstrip("/"), **common, **kw)


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = _build_client()
    return _client


# ------------------------------------------------------------- guardrails ---


class Denied(Exception):
    pass


def _require(level: int, tool: str) -> None:
    if _LEVEL < level:
        raise Denied(
            f"tool '{tool}' is disabled by PODMAN_MCP_PROFILE={PROFILE}. "
            f"Requires profile '{'full' if level == 2 else 'safe'}' or higher."
        )


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _check_name(name: str) -> str:
    """
    收斂成 podman 合法名稱/ID 字元集。舊版只擋 '/' 與開頭 '-'，
    但 '?' 之類的字元會竄改請求 URL 結構（container?timeout=0 → 405）。
    """
    name = (name or "").strip()
    if not name:
        raise Denied("name/ID must not be empty")
    if not _SAFE_NAME_RE.match(name):
        raise Denied(
            f"invalid container name/ID {name!r} "
            "(allowed: letters, digits, and _ . - after a leading alphanumeric)"
        )
    if _NAME_RE and not _NAME_RE.search(name):
        raise Denied(
            f"name {name!r} is not permitted by PODMAN_MCP_NAME_ALLOW={NAME_ALLOW!r}"
        )
    return name


# ----------------------------------------------------------- http helpers ---


def _p(path: str) -> str:
    """libpod 路徑一定要 /v<version> 前綴，沒有前綴會 404。"""
    return f"/{API_VERSION}/libpod{path}"


def _raise_for_podman(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    try:
        body = r.json()
        cause = body.get("cause") or ""
        msg = body.get("message") or ""
        raise RuntimeError(f"podman API {r.status_code}: {msg or cause}")
    except (json.JSONDecodeError, ValueError):
        # 路由沒中的 404/405 是 plain text，不是 ErrorModel
        raise RuntimeError(f"podman API {r.status_code}: {r.text.strip()[:300]}")


async def _get(path: str, **params: Any) -> Any:
    r = await client().get(_p(path), params=_clean(params))
    _raise_for_podman(r)
    return r.json() if r.content else None


async def _post(path: str, *, params: Any = None, json_body: Any = None) -> httpx.Response:
    r = await client().post(_p(path), params=_clean(params or {}), json=json_body)
    _raise_for_podman(r)
    return r


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        out[k] = "true" if v is True else ("false" if v is False else v)
    return out


def _filters(v: Any) -> str | None:
    """filters 允許 dict 或 JSON 字串；LLM 兩種都會給。"""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        norm = {k: (val if isinstance(val, list) else [str(val)]) for k, val in v.items()}
        return json.dumps(norm)
    raise Denied(f"filters must be a JSON object or JSON string, got {type(v).__name__}")


DEFAULT_HINT = "request a narrower scope, or raise PODMAN_MCP_MAX_CHARS"


def _cap(text: str, limit: int | None = None, *, hint: str = DEFAULT_HINT) -> str:
    """
    截斷提示一定要說出「這個工具真的有的」收斂手段，所以 hint 由呼叫端傳。
    注意:_table 已自行處理列預算,不要再對 _table 的輸出 _cap 一次(會蓋掉內層 marker)。
    """
    limit = limit or MAX_CHARS
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n… [truncated {dropped} chars — {hint}]"


def _dump(obj: Any, *, hint: str = DEFAULT_HINT) -> str:
    """
    list 超量時回「合法的 JSON 信封」而不是把序列化字串攔腰砍斷。
    dict 無法安全逐元素切,維持字元截斷,但 hint 會講清楚怎麼縮小範圍。
    """
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    if len(text) <= MAX_CHARS:
        return text
    if isinstance(obj, list):
        budget = MAX_CHARS - 400
        kept: list[Any] = []
        used = 0
        for item in obj:
            size = len(json.dumps(item, ensure_ascii=False)) + 2
            if used + size > budget:
                break
            kept.append(item)
            used += size
        return json.dumps(
            {
                "items": kept,
                "truncated": True,
                "total": len(obj),
                "returned": len(kept),
                "hint": hint,
            },
            indent=2,
            ensure_ascii=False,
        )
    return _cap(text, hint=hint)


def _table(
    rows: list[list[str]],
    headers: list[str],
    *,
    hint: str = DEFAULT_HINT,
    keep: str = "head",
) -> str:
    """
    超量時以「整列」為單位裁掉並回報丟了幾列(對 LLM 遠比丟了幾個字元有用)。
    keep="tail" 保留最新的列(事件流要的是最新,不是最舊)。
    欄數與表頭不符的 row 會被補齊/裁齊,避免 str.format 丟出 Replacement index 錯誤。
    """
    if not rows:
        return "(none)"
    ncol = len(headers)
    norm = [[str(c) for c in row[:ncol]] + [""] * max(0, ncol - len(row)) for row in rows]
    widths = [len(h) for h in headers]
    for row in norm:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    head = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    data = [fmt.format(*r) for r in norm]

    body = "\n".join(head + data)
    if len(body) <= MAX_CHARS:
        return body

    budget = MAX_CHARS - len(head[0]) - len(head[1]) - 200
    ordered = list(reversed(data)) if keep == "tail" else data
    kept: list[str] = []
    used = 0
    for line in ordered:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    if keep == "tail":
        kept.reverse()
    dropped = len(data) - len(kept)
    which = "oldest" if keep == "tail" else "remaining"
    marker = f"… [{dropped} of {len(data)} rows omitted ({which}) — {hint}]"
    return "\n".join(head + kept + [marker])


def _short(s: str | None, n: int = 12) -> str:
    return (s or "")[:n]


def _trunc(s: str | None, n: int, *, tail: bool = False) -> str:
    """截斷「識別字串」時一定要留下 … 標記,否則 LLM 會拿一個看起來合法的錯值去打下一個 API。"""
    s = s or ""
    if len(s) <= n:
        return s
    return "…" + s[-(n - 1):] if tail else s[: n - 1] + "…"


def _ago(ts: Any) -> str:
    if not ts:
        return "-"
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return str(ts)[:19]
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    for unit, div in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= div:
            return f"{int(secs // div)}{unit} ago"
    return f"{int(secs)}s ago"


def _human(n: Any) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _demux(raw: bytes, *, keep_stderr: bool = True, muxed: bool = True) -> str:
    """
    libpod 的 logs / exec / attach 是 8-byte header framing:
      [0]=stream(1=stdout,2=stderr) [1:4]=0 [4:8]=big-endian uint32 payload len

    muxed=False 直接當 raw 解碼 —— 呼叫端知道 tty=True 時務必傳,
    不要讓下面的啟發式去猜(raw 輸出剛好長得像 header 時會被靜默吃掉 8 個 byte)。

    "E| " 前綴只在「行首」貼。stderr 跨 frame 時,上一個 frame 可能停在行中間,
    此時不能再貼前綴,否則會插進行的中央污染內容 —— 所以要跨 frame 記住行首狀態。
    """
    if not muxed:
        return raw.decode("utf-8", "replace")

    out: list[str] = []
    i, n = 0, len(raw)
    at_line_start = True  # 只針對 stderr;stdout 不加前綴所以不需要
    if n and raw[0] not in (0, 1, 2):
        return raw.decode("utf-8", "replace")
    while i + 8 <= n:
        stream = raw[i]
        if stream not in (0, 1, 2) or raw[i + 1 : i + 4] != b"\x00\x00\x00":
            return raw.decode("utf-8", "replace")
        length = int.from_bytes(raw[i + 4 : i + 8], "big")
        i += 8
        chunk = raw[i : i + length]
        i += length
        if stream == 2 and not keep_stderr:
            continue
        text = chunk.decode("utf-8", "replace")
        if stream != 2:
            out.append(text)
            continue
        parts = text.splitlines(keepends=True)
        for idx, ln in enumerate(parts):
            out.append(("E| " + ln) if (at_line_start or idx > 0) else ln)
            at_line_start = ln.endswith(("\n", "\r"))
        if not parts:
            continue
    if i < n:
        out.append(raw[i:].decode("utf-8", "replace"))
    return "".join(out)


# ------------------------------------------------------------------ server --

def _transport_security() -> TransportSecuritySettings | None:
    """
    SDK 預設只放行 localhost 的 Host header（DNS rebinding 防護），
    放在 Cloudflare Tunnel / ingress 後面時外部 hostname 會被擋成 421 Invalid Host header。
    PODMAN_MCP_ALLOWED_HOSTS 逗號分隔要放行的 hostname；設 "*" 直接關掉防護。
    """
    hosts = [h.strip() for h in os.getenv("PODMAN_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return None
    if hosts == ["*"]:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    local = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    return TransportSecuritySettings(
        allowed_hosts=hosts + local,
        allowed_origins=[f"https://{h}" for h in hosts]
        + [f"http://{h}" for h in hosts]
        + [f"http://{h}" for h in local],
    )


mcp = FastMCP(
    "podman",
    transport_security=_transport_security(),
    instructions=(
        "Manage a Podman host through its libpod REST API. "
        f"Active safety profile: {PROFILE}. "
        "Prefer podman_ps / podman_container_logs before acting. "
        "Container inspect output is large — use podman_ps first and inspect one target."
    ),
)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WR = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def tool(level: int, **kw: Any):
    """level 0=readonly 1=safe 2=full；profile 不夠就不註冊（protocol 層看不到）。"""

    def deco(fn):
        if _LEVEL >= level:
            return mcp.tool(**kw)(fn)
        return fn

    return deco


# ------------------------------------------------------------ read tools ----


@tool(0, annotations=RO, description="Podman host 概況：版本、OS、runtime、rootless、資源用量。")
async def podman_info(format: Literal["text", "json"] = "text") -> str:
    d = await _get("/info")
    if format == "json":
        return _dump(d)
    h, v, s = d.get("host", {}), d.get("version", {}), d.get("store", {})
    return _cap(
        "\n".join(
            [
                f"podman        {v.get('Version')} (api {v.get('APIVersion')})",
                f"os/arch       {h.get('os')}/{h.get('arch')}  kernel {h.get('kernel')}",
                f"rootless      {h.get('security', {}).get('rootless')}",
                f"runtime       {h.get('ociRuntime', {}).get('name')}  network={h.get('networkBackend')}",
                f"cgroups       {h.get('cgroupVersion')} ({h.get('cgroupManager')})",
                f"cpus/mem      {h.get('cpus')} cpu / {_human(h.get('memTotal'))} total, {_human(h.get('memFree'))} free",
                f"store         {s.get('graphDriverName')} at {s.get('graphRoot')}",
                f"counts        {s.get('containerStore', {}).get('number')} containers, {s.get('imageStore', {}).get('number')} images",
            ]
        )
    )


@tool(0, annotations=RO, description="列出 containers。預設只列 running，all=true 含已停止。")
async def podman_ps(
    all: bool = False,
    filters: str | dict[str, Any] | None = None,
    format: Literal["text", "json"] = "text",
) -> str:
    """filters: libpod filter JSON, e.g. {"status":["running"],"label":["app=web"]}"""
    d = await _get("/containers/json", all=all, filters=_filters(filters))
    if format == "json":
        return _dump(d)
    rows = [
        [
            _short(c.get("Id")),
            (c.get("Names") or ["-"])[0],
            _trunc(c.get("Image") or "-", 40, tail=True),
            c.get("State") or "-",
            _ago(c.get("Created")),
            c.get("PodName") or "-",
            ",".join(
                f"{p.get('host_port')}->{p.get('container_port')}/{p.get('protocol')}"
                for p in (c.get("Ports") or [])
            )
            or "-",
        ]
        for c in (d or [])
    ]
    return _table(rows, ["ID", "NAME", "IMAGE", "STATE", "CREATED", "POD", "PORTS"],
                  hint="use filters= or all=false")


@tool(0, annotations=RO, description="Inspect 單一 container 的完整設定/狀態。輸出很大，只查你要的那一個。")
async def podman_container_inspect(name: str, section: str | None = None) -> str:
    """section: 只取某個 top-level key，例如 State / Config / HostConfig / NetworkSettings / Mounts"""
    d = await _get(f"/containers/{_check_name(name)}/json")
    if section:
        if section not in d:
            return f"no such section {section!r}. available: {', '.join(sorted(d))}"
        d = {section: d[section]}
    return _dump(d, hint="use section= (State/Config/HostConfig/NetworkSettings/Mounts)")


@tool(
    0,
    annotations=RO,
    description="取 container logs。預設只取最後 200 行；stderr 行會以 'E| ' 前綴標示。",
)
async def podman_container_logs(
    name: str,
    tail: int = 200,
    since: str | None = None,
    until: str | None = None,
    timestamps: bool = False,
    stdout: bool = True,
    stderr: bool = True,
    grep: str | None = None,
) -> str:
    """since/until: unix timestamp 或 Go duration (e.g. '10m')。grep: 只回符合的行 (regex)。"""
    if not (stdout or stderr):
        raise Denied("at least one of stdout/stderr must be true")
    r = await client().get(
        _p(f"/containers/{_check_name(name)}/logs"),
        params=_clean(
            {
                "stdout": stdout,
                "stderr": stderr,
                "tail": str(tail) if tail and tail > 0 else "all",
                "since": since,
                "until": until,
                "timestamps": timestamps,
                "follow": False,
            }
        ),
    )
    _raise_for_podman(r)
    text = _demux(r.content, keep_stderr=stderr)
    lines = text.splitlines()
    if grep:
        try:
            rx = re.compile(grep)
        except re.error as e:
            raise Denied(f"invalid regex in grep={grep!r}: {e}")
        # 對「脫掉 E| 前綴」的內容比對,否則 ^ 錨定的 regex 對 stderr 行永遠不會中
        kept = [ln for ln in lines if rx.search(ln[3:] if ln.startswith("E| ") else ln)]
        if not kept:
            return f"(no lines matched grep {grep!r}; {len(lines)} lines scanned)"
        text = "\n".join(kept)
    return _cap(text or "(no log output)", hint="use tail=, since=, or grep=")


@tool(0, annotations=RO, description="container 即時資源用量（單次快照，不 streaming）。")
async def podman_container_stats(names: list[str] | None = None, all: bool = False) -> str:
    params: list[tuple[str, str]] = [("stream", "false"), ("all", "true" if all else "false")]
    for n in names or []:
        params.append(("containers", _check_name(n)))
    r = await client().get(_p("/containers/stats"), params=params)
    _raise_for_podman(r)
    payload = r.json()
    if isinstance(payload, dict) and payload.get("Error") is not None:
        # libpod 的 /containers/stats 只要有一個名字查無,就回 HTTP 200 +
        # {"Error":{}, "Stats":null} —— 注意 Error 是**空 dict**(falsy),成功時才是 None。
        # 不攔的話整批結果會靜默變成 "(none)",呼叫者會以為容器都沒在跑。
        err = payload["Error"]
        msg = (err.get("message") or err.get("cause")) if isinstance(err, dict) else err
        # libpod 在這種情況不回哪一個名字有問題,Stats 直接是 null。
        # 所以只能說「這批裡至少有一個查無」,不能指名 —— 指名會誣賴存在的容器。
        raise RuntimeError(
            f"podman stats failed for names={names!r}"
            + (f": {msg}" if msg else "")
            + " — libpod returns no stats at all when ANY requested name is unknown;"
              " retry with one name at a time to find the bad one, or use all=true"
        )
    stats = payload.get("Stats") if isinstance(payload, dict) else payload
    rows = [
        [
            _short(s.get("ContainerID")),
            s.get("Name") or "-",
            f"{s.get('CPU', 0):.2f}%",
            f"{_human(s.get('MemUsage'))}/{_human(s.get('MemLimit'))}",
            f"{s.get('MemPerc', 0):.2f}%",
            f"{_human(s.get('NetInput'))}/{_human(s.get('NetOutput'))}",
            str(s.get("PIDs", "-")),
        ]
        for s in (stats or [])
    ]
    return _table(rows, ["ID", "NAME", "CPU", "MEM", "MEM%", "NET I/O", "PIDS"],
                  hint="pass fewer names= or all=false")


@tool(0, annotations=RO, description="列出 container 內的 process (podman top)。")
async def podman_container_top(name: str, ps_args: str | None = None) -> str:
    d = await _get(f"/containers/{_check_name(name)}/top", **({"ps_args": ps_args} if ps_args else {}))
    titles = list(d.get("Titles") or [])
    procs = [[str(c) for c in row] for row in (d.get("Processes") or [])]
    if procs and titles and any(len(r) != len(titles) for r in procs):
        # 帶 ps 旗標(aux / -ef)時 libpod 會回「未切欄的整行」,欄數對不上表頭。
        # 硬塞進 _table 會排出 100+ 字元寬的假欄位,不如直接印原始行。
        return _cap(
            "  ".join(titles) + "\n" + "\n".join("  ".join(r) for r in procs),
            hint="libpod did not split columns for these ps flags; "
                 "use descriptor form like ps_args='-eo pid,user,comm' for a real table",
        )
    return _table(procs, titles, hint="narrow ps_args=")


@tool(0, annotations=RO, description="執行並回報 container 的 healthcheck 結果。")
async def podman_container_healthcheck(name: str) -> str:
    d = await _get(f"/containers/{_check_name(name)}/healthcheck")
    log = d.get("Log") or []
    lines = [f"status={d.get('Status')} failingStreak={d.get('FailingStreak')}"]
    if log:
        last = log[-1]
        lines.append(f"last exit={last.get('ExitCode')} at {last.get('End')}")
        out = (last.get("Output") or "").strip()
        lines.append(f"output:\n{out}" if out else "output: (empty)")
    else:
        lines.append("(no healthcheck log entries yet)")
    return _cap("\n".join(lines))


@tool(0, annotations=RO, description="列出本機 images。")
async def podman_images(
    all: bool = False, filters: str | dict[str, Any] | None = None, format: Literal["text", "json"] = "text"
) -> str:
    d = await _get("/images/json", all=all, filters=_filters(filters))
    # libpod 不保證順序;不排序的話配上截斷等於每次回不同的隨機子集
    d = sorted(d or [], key=lambda i: (i.get("RepoTags") or i.get("Names") or ["<none>"])[0])
    if format == "json":
        return _dump(d, hint="use filters= (reference/dangling/label)")
    rows = [
        [
            _short((i.get("Id") or "").replace("sha256:", "")),
            _trunc(", ".join(i.get("RepoTags") or i.get("Names") or ["<none>"]), 60),
            _ago(i.get("Created")),
            _human(i.get("Size")),
            str(i.get("Containers", 0)),
        ]
        for i in (d or [])
    ]
    return _table(rows, ["ID", "TAGS", "CREATED", "SIZE", "USED BY"],
                  hint="use filters= (reference/dangling/label)")


@tool(0, annotations=RO, description="列出 pods 與其中的 containers。")
async def podman_pods(filters: str | dict[str, Any] | None = None, format: Literal["text", "json"] = "text") -> str:
    d = await _get("/pods/json", filters=_filters(filters))
    if format == "json":
        return _dump(d)
    rows = [
        [
            _short(p.get("Id")),
            p.get("Name") or "-",
            p.get("Status") or "-",
            _ago(p.get("Created")),
            str(len(p.get("Containers") or [])),
            ", ".join(c.get("Names", "") for c in (p.get("Containers") or []))[:50],
        ]
        for p in (d or [])
    ]
    return _table(rows, ["ID", "NAME", "STATUS", "CREATED", "#CTR", "CONTAINERS"])


@tool(0, annotations=RO, description="列出 volumes。")
async def podman_volumes(format: Literal["text", "json"] = "text") -> str:
    d = sorted(await _get("/volumes/json") or [], key=lambda v: v.get("Name", ""))
    if format == "json":
        return _dump(d, hint="raise PODMAN_MCP_MAX_CHARS, or use format='text'")
    rows = [
        # NAME 是 volume 唯一的識別欄(沒有 ID),截斷會產生看起來重複的列 —— 完整輸出
        [v.get("Name", "-"), v.get("Driver", "-"), _ago(v.get("CreatedAt")),
         _trunc(v.get("Mountpoint") or "-", 50, tail=True)]
        for v in (d or [])
    ]
    return _table(rows, ["NAME", "DRIVER", "CREATED", "MOUNTPOINT"],
                  hint="raise PODMAN_MCP_MAX_CHARS to see all volumes")


@tool(0, annotations=RO, description="列出 networks。")
async def podman_networks(format: Literal["text", "json"] = "text") -> str:
    d = sorted(await _get("/networks/json") or [], key=lambda n: n.get("name", ""))
    if format == "json":
        return _dump(d, hint="raise PODMAN_MCP_MAX_CHARS, or use format='text'")
    rows = [
        [
            n.get("name", "-"),
            n.get("driver", "-"),
            ", ".join(s.get("subnet", "") for s in (n.get("subnets") or [])) or "-",
            str(n.get("internal", False)),
        ]
        for n in (d or [])
    ]
    return _table(rows, ["NAME", "DRIVER", "SUBNETS", "INTERNAL"],
                  hint="raise PODMAN_MCP_MAX_CHARS to see all networks")


@tool(0, annotations=RO, description="磁碟用量統計 (podman system df)。")
async def podman_system_df() -> str:
    d = await _get("/system/df")
    lines = [f"images total     {_human(d.get('ImagesSize'))} across {len(d.get('Images') or [])} images"]
    lines.append(f"containers       {len(d.get('Containers') or [])}")
    lines.append(
        f"volumes          {len(d.get('Volumes') or [])}, reclaimable "
        f"{_human(sum(v.get('ReclaimableSize', 0) for v in (d.get('Volumes') or [])))}"
    )
    lines.append("")
    lines.append(
        _table(
            [
                [
                    _trunc(i.get("Repository") or "-", 40, tail=True),
                    i.get("Tag") or "-",
                    _human(i.get("Size")),
                    _human(i.get("UniqueSize")),
                    str(i.get("Containers", 0)),
                ]
                for i in (d.get("Images") or [])
            ],
            ["REPOSITORY", "TAG", "SIZE", "UNIQUE", "USED BY"],
            hint="the 3-line summary above is computed from the full payload and stays accurate",
        )
    )
    # ★ 不要再 _cap 一次 —— _table 已依列預算裁過並留下 marker,
    #   外層再 cap 會把內層 marker 切掉,讓 dropped 數字看起來小一個數量級。
    return "\n".join(lines)


@tool(
    0,
    annotations=RO,
    description="讀取 podman 事件流（有界，不會卡住）。查故障時間線很好用。",
)
async def podman_events(since: str = "30m", until: str | None = None, filters: str | dict[str, Any] | None = None, limit: int = 100) -> str:
    """
    since: Go duration ('30m') 或 unix timestamp。
    until: **只建議用 unix timestamp**；Go duration 形式在 libpod 這一版會靜默失效，
           而 until="0s" 只會回一筆。不確定就別給 until。
    stream=false 是關鍵：它讓 server 回放歷史後直接關閉連線，不會卡住。
    """
    if limit < 1:
        raise Denied(f"limit must be >= 1 (got {limit})")
    r = await client().get(
        _p("/events"),
        params=_clean({"since": since, "until": until, "filters": _filters(filters), "stream": False}),
    )
    _raise_for_podman(r)
    rows = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        # libpod 對不合法的 since / filter key 會在串流裡吐 ErrorModel。
        # 不攔的話會被印成一筆「所有欄位都是 -」的假事件,呼叫者以為查詢成功。
        if isinstance(e, dict) and "response" in e and ("cause" in e or "message" in e):
            raise RuntimeError(
                f"podman API {e.get('response')}: {e.get('message') or e.get('cause')}"
            )
        if not isinstance(e, dict) or not (e.get("Type") or e.get("Action")):
            continue
        rows.append(
            [
                _ago(e.get("time")),
                e.get("Type", "-"),
                e.get("Action", "-"),
                (e.get("Actor", {}).get("Attributes", {}).get("name") or e.get("Name") or "-")[:30],
                _short(e.get("Actor", {}).get("ID")),
            ]
        )
    if not rows:
        return f"(no events matched since={since!r}" + (f" until={until!r}" if until else "") + ")"
    rows = rows[-limit:]  # tail 語意:取最新 N 筆
    # keep="tail" —— 超量時丟掉最舊的列。查故障時間線要的是最新事件,
    # 預設從頭截會把最新的全部丟掉,結果剛好相反。
    return _table(rows, ["WHEN", "TYPE", "ACTION", "NAME", "ID"],
                  keep="tail", hint="lower limit= or narrow since=/filters=")


# ------------------------------------------------------- lifecycle (safe) ---


@tool(1, annotations=WR, description="啟動 container。")
async def podman_container_start(name: str) -> str:
    _require(1, "podman_container_start")
    n = _check_name(name)
    r = await _post(f"/containers/{n}/start")
    return f"{n}: {'already running' if r.status_code == 304 else 'started'}"


@tool(1, annotations=WR, description="停止 container（預設 10 秒後 SIGKILL）。")
async def podman_container_stop(name: str, timeout: int = 10) -> str:
    _require(1, "podman_container_stop")
    n = _check_name(name)
    r = await _post(f"/containers/{n}/stop", params={"timeout": timeout})
    return f"{n}: {'already stopped' if r.status_code == 304 else 'stopped'}"


@tool(1, annotations=WR, description="重啟 container。")
async def podman_container_restart(name: str, timeout: int = 10) -> str:
    _require(1, "podman_container_restart")
    n = _check_name(name)
    await _post(f"/containers/{n}/restart", params={"timeout": timeout})
    return f"{n}: restarted"


@tool(
    1,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
    description=("在既有 container 內執行指令，回傳 stdout/stderr 與 exit code。"
                 " stderr 行會以 'E| ' 前綴標示（tty=true 時為 raw、無前綴）。"),
)
async def podman_container_exec(
    name: str,
    command: str,
    user: str | None = None,
    workdir: str | None = None,
    tty: bool = False,
) -> str:
    """command 用 shell 語法字串，內部以 shlex 拆解；要 shell 特性請自己包 sh -c '...'。"""
    _require(1, "podman_container_exec")
    n = _check_name(name)
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise Denied(f"cannot parse command: {e}")
    if not argv:
        raise Denied("empty command")

    body: dict[str, Any] = {
        "AttachStdout": True,
        "AttachStderr": True,
        "Cmd": argv,
        "Tty": tty,
    }
    if user:
        body["User"] = user
    if workdir:
        body["WorkingDir"] = workdir

    r = await _post(f"/containers/{n}/exec", json_body=body)
    exec_id = r.json()["Id"]

    start = await client().post(
        _p(f"/exec/{exec_id}/start"), json={"Detach": False, "Tty": tty}
    )
    _raise_for_podman(start)
    # tty=True 時 podman 回 raw stream。我們已經知道 tty,就不要讓 _demux 用啟發式去猜 ——
    # raw 輸出剛好長得像 8-byte header 時會被靜默吃掉 8 個 byte。
    output = _demux(start.content, muxed=not tty)

    info = await _get(f"/exec/{exec_id}/json")
    code = info.get("ExitCode")
    return _cap(
        f"exit code: {code}\n---\n{output or '(no output)'}",
        hint="add head/tail/grep inside the command itself to limit output",
    )


@tool(
    1,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    description="從 registry 拉 image。會等到拉完，大 image 請留意 timeout。",
)
async def podman_image_pull(reference: str, tls_verify: bool = True, policy: str = "missing") -> str:
    _require(1, "podman_image_pull")
    if not reference or reference.startswith("-"):
        raise Denied("invalid reference")
    r = await client().post(
        _p("/images/pull"),
        params=_clean({"reference": reference, "tlsVerify": tls_verify, "policy": policy}),
        timeout=httpx.Timeout(max(TIMEOUT, 600.0)),
    )
    _raise_for_podman(r)
    # NDJSON 串流；錯誤可能在 200 之後才出現在 error 欄位
    ids, errors, last = [], [], ""
    for line in r.text.splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("error"):
            errors.append(o["error"])
        if o.get("images"):
            ids += o["images"]
        if o.get("stream"):
            last = o["stream"].strip()
    if errors:
        raise RuntimeError("pull failed: " + "; ".join(errors))
    return f"pulled {reference} -> {', '.join(_short(i) for i in ids) or last or 'ok'}"


# ------------------------------------------------------ destructive (full) --


@tool(2, annotations=DES, description="建立並啟動一個新 container（等同 podman run -d）。")
async def podman_container_run(
    image: str,
    name: str | None = None,
    command: str | None = None,
    env: dict[str, str] | None = None,
    ports: list[str] | None = None,
    volumes: list[str] | None = None,
    network: str | None = None,
    labels: dict[str, str] | None = None,
    remove: bool = False,
    privileged: bool = False,
) -> str:
    """ports: ["8080:80", "127.0.0.1:5432:5432/tcp"]；volumes: ["/host/path:/ctr/path:ro"]。"""
    _require(2, "podman_container_run")
    if privileged and os.getenv("PODMAN_MCP_ALLOW_PRIVILEGED") != "1":
        raise Denied("privileged=true requires PODMAN_MCP_ALLOW_PRIVILEGED=1 on the server")
    spec: dict[str, Any] = {"image": image, "remove": remove, "privileged": privileged}
    if name:
        spec["name"] = _check_name(name)
    if command:
        spec["command"] = shlex.split(command)
    if env:
        spec["env"] = env
    if labels:
        spec["labels"] = labels
    if network:
        spec["netns"] = {"nsmode": "bridge"}
        spec["Networks"] = {network: {}}
    if ports:
        mappings = []
        for p in ports:
            proto = "tcp"
            spec_p = p
            if "/" in spec_p:
                spec_p, proto = spec_p.rsplit("/", 1)
            parts = spec_p.split(":")
            if len(parts) == 3:
                host_ip, host_p, ctr_p = parts
            elif len(parts) == 2:
                host_ip, (host_p, ctr_p) = "", parts
            else:
                raise Denied(f"bad port spec {p!r}")
            m = {"host_port": int(host_p), "container_port": int(ctr_p), "protocol": proto}
            if host_ip:
                m["host_ip"] = host_ip
            mappings.append(m)
        spec["portmappings"] = mappings
    if volumes:
        named, mounts = [], []
        for v in volumes:
            parts = v.split(":")
            if len(parts) < 2:
                raise Denied(f"bad volume spec {v!r}")
            src, dst = parts[0], parts[1]
            opts = parts[2].split(",") if len(parts) > 2 else []
            if src.startswith("/") or src.startswith("."):
                mounts.append({"Type": "bind", "Source": src, "Destination": dst, "Options": opts})
            else:
                named.append({"Name": src, "Dest": dst, "Options": opts})
        if mounts:
            spec["mounts"] = mounts
        if named:
            spec["volumes"] = named

    r = await _post("/containers/create", json_body=spec)
    cid = r.json()["Id"]
    warnings = r.json().get("Warnings") or []
    await _post(f"/containers/{cid}/start")
    return f"started {name or _short(cid)} ({_short(cid)}) from {image}" + (
        f"\nwarnings: {warnings}" if warnings else ""
    )


@tool(2, annotations=DES, description="送 signal 給 container（預設 SIGKILL）。")
async def podman_container_kill(name: str, signal: str = "SIGKILL") -> str:
    _require(2, "podman_container_kill")
    n = _check_name(name)
    await _post(f"/containers/{n}/kill", params={"signal": signal})
    return f"{n}: sent {signal}"


@tool(2, annotations=DES, description="刪除 container。force=true 會先停掉。")
async def podman_container_remove(name: str, force: bool = False, volumes: bool = False) -> str:
    _require(2, "podman_container_remove")
    n = _check_name(name)
    r = await client().delete(_p(f"/containers/{n}"), params=_clean({"force": force, "v": volumes}))
    _raise_for_podman(r)
    return f"{n}: removed"


@tool(2, annotations=DES, description="刪除 image。")
async def podman_image_remove(name: str, force: bool = False) -> str:
    _require(2, "podman_image_remove")
    r = await client().delete(_p(f"/images/{name}"), params=_clean({"force": force}))
    _raise_for_podman(r)
    d = r.json()
    return _cap(f"deleted: {d.get('Deleted')}\nuntagged: {d.get('Untagged')}\nerrors: {d.get('Errors')}")


@tool(2, annotations=DES, description="清理未使用資源 (podman system prune)。預設不含 volumes。")
async def podman_system_prune(all: bool = False, volumes: bool = False, filters: str | dict[str, Any] | None = None) -> str:
    _require(2, "podman_system_prune")
    r = await _post("/system/prune", params={"all": all, "volumes": volumes, "filters": _filters(filters)})
    d = r.json()
    return _cap(
        f"reclaimed {_human(d.get('ReclaimedSpace'))}\n"
        f"containers={len(d.get('ContainerPruneReports') or [])} "
        f"images={len(d.get('ImagePruneReports') or [])} "
        f"volumes={len(d.get('VolumePruneReports') or [])} "
        f"networks={len(d.get('NetworkPruneReports') or [])} "
        f"pods={len(d.get('PodPruneReport') or [])}"
    )


# -------------------------------------------------------------------- main --


def main() -> None:
    ap = argparse.ArgumentParser(description="Podman MCP server")
    ap.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="stdio for a local client; streamable-http when the admin console proxies to us",
    )
    ap.add_argument("--host", default="127.0.0.1", help="bind address (streamable-http only)")
    ap.add_argument("--port", type=int, default=8000, help="bind port (streamable-http only)")
    ap.add_argument(
        "--path",
        default="/mcp",
        help=(
            "HTTP path to mount the MCP endpoint on (streamable-http only). "
            "The admin console's reverse proxy strips its own /private_<token> "
            "prefix before forwarding, so this stays a plain /mcp in normal use."
        ),
    )
    # Back-compat with the single-file deployment: --http == --transport streamable-http.
    ap.add_argument("--http", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    transport = "streamable-http" if args.http else args.transport

    print(
        f"[podman-mcp] uri={PODMAN_URI} api={API_VERSION} profile={PROFILE} "
        f"tools={len(mcp._tool_manager.list_tools())} transport={transport}",
        file=sys.stderr,
    )
    if transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.streamable_http_path = args.path
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
