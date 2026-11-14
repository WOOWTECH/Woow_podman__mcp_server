"""MCP 子行程的作業系統身分：spawn、process group、loopback listener（blocker 3）。

閘道只轉發到 ``127.0.0.1:<mcp_server.port>``。過去「那個 port 有東西在回應」就被當成受管子行程：
被別的程序（舊子行程留下的孫行程、另一個 profile 的子行程、事先佔住 port 的程序）佔住時，MCP 流量與
proxy 的內部 bearer 會送到它那裡。這個模組提供管理程序用來確認身分的作業系統層工具，政策在
``process.py``（什麼時候檢查、失敗怎麼收斂）與 ``proxy.py``（沒有通過檢查的世代不轉發）：

- **spawn**：子行程一律在自己的 session／process group（``start_new_session``，C 層 setsid，不跑 Python
  preexec）。Linux 上再經一個極小的 exec 包裝：``prctl(PR_SET_PDEATHSIG, SIGKILL)`` 之後檢查
  ``getppid()`` 仍是管理程序（關掉 fork 與 prctl 之間管理程序先死的競爭），再 ``execv`` 真正的命令。
  不用 ``preexec_fn``：管理程序有執行緒（``asyncio.to_thread``），fork 之後在子行程跑 Python 碼不安全；
  包裝是全新的單執行緒直譯器（``-I -S``），只多一次約 15 ms 的啟動，env 原封不動交給真正的命令
  （PEP 538 會改 ``LC_CTYPE``，包裝 exec 前還原）。PDEATHSIG 綁的是 fork 的那個「執行緒」：asyncio 在
  事件迴圈執行緒呼叫 ``Popen``（uvicorn 是主執行緒），該執行緒結束＝管理程序結束。
- **process group**：只在 leader 還沒被收屍時 ``killpg``（leader 是 session leader，不能離開自己的
  group；還沒收屍的 PID 不會被重用，所以 PGID 一定是我們建立的那一個）。leader 收屍之後只在「這一次
  /proc 掃描剛看到成員還活著」時才 ``killpg``——成員還在就佔著這個 PGID，號碼不可能已被重用。
- **port**：spawn 前以 ``SO_REUSEADDR`` 試綁 ``0.0.0.0:P`` 與 ``[::]:P``（v6only），任何聽著或綁著
  （不可重用）的 socket 都會讓試綁失敗；另外列出管理程序自己的 listener port。
- **peer**：解析 ``/proc/net/tcp``／``tcp6``，找出會接受 ``127.0.0.1:P`` 連線的 LISTEN socket，再由
  ``/proc/<pid>/fd`` 找出持有它的程序，每一個都必須在子行程的 session（session id＝子行程 PID）。

不是 Linux（沒有 ``/proc/net/tcp``）時：spawn 只開新 session、peer 檢查回 ``UNSUPPORTED``（呼叫端記
warning 後沿用原行為）；正式環境是 Linux。
"""

from __future__ import annotations

import enum
import errno
import ipaddress
import os
import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

LINUX = sys.platform.startswith("linux")
_PROC = Path("/proc")
_TCP_LISTEN = "0A"
#: 會接受 ``127.0.0.1:P`` 連線的本機位址（IPv6 的 ``::`` 是否 v6only 從 /proc 看不出來，保守算進去）。
_ACCEPTS_LOOPBACK = frozenset({
    ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("0.0.0.0"),
    ipaddress.ip_address("::"), ipaddress.ip_address("::ffff:127.0.0.1"), ipaddress.ip_address("::ffff:0.0.0.0"),
})

# ---------------------------------------------------------------------------
# spawn：自己的 session＋parent-death signal
# ---------------------------------------------------------------------------

#: exec 包裝（``python -I -S -c``）。argv：期望的父程序 PID、原本的 LC_CTYPE（``-``＝沒有；``=值``）、
#: 要 exec 的路徑、真正的 argv。125＝PDEATHSIG 設不上或管理程序已經不在；127＝exec 失敗。
#: 直譯器啟動時把 SIGPIPE／SIGXFZ／SIGXFSZ 設成忽略，而「忽略」會跨 exec 繼承：exec 前還原成預設
#: （與 Popen 的 ``restore_signals`` 相同，真正的命令看到的訊號處置與不經包裝時一樣）。
_PDEATHSIG_EXEC = r"""import ctypes,os,signal,sys
ppid=int(sys.argv[1]);lc=sys.argv[2];path=sys.argv[3]
libc=ctypes.CDLL(None,use_errno=True)
libc.prctl.argtypes=(ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong)
if libc.prctl(1,9,0,0,0)!=0:
 os.write(2,b'mcp-admin: PR_SET_PDEATHSIG failed\n');os._exit(125)
if os.getppid()!=ppid:os._exit(125)
if lc=='-':os.environ.pop('LC_CTYPE',None)
else:os.environ['LC_CTYPE']=lc[1:]
for n in('SIGPIPE','SIGXFZ','SIGXFSZ'):
 if hasattr(signal,n):signal.signal(getattr(signal,n),signal.SIG_DFL)
try:os.execv(path,sys.argv[4:])
except OSError as e:
 os.write(2,('mcp-admin: cannot execute the MCP child: %s\n'%(e.strerror or e)).encode());os._exit(127)
"""


def spawn_argv(command: str, args: list[str], env: dict[str, str]) -> tuple[list[str], bool]:
    """實際要交給 ``create_subprocess_exec`` 的 argv，與子行程是否會收到 parent-death SIGKILL。

    命令照 ``Popen`` 的規則（env 的 PATH）找不到時原樣回傳：spawn 會像以前一樣丟 ``FileNotFoundError``。
    """
    if not LINUX or not sys.executable:
        return [command, *args], False
    program = _resolve(command, env)
    if program is None:
        return [command, *args], False
    lc_ctype = "=" + env["LC_CTYPE"] if "LC_CTYPE" in env else "-"
    return [sys.executable, "-I", "-S", "-c", _PDEATHSIG_EXEC, str(os.getpid()), lc_ctype, program,
            command, *args], True


def _resolve(command: str, env: dict[str, str]) -> str | None:
    if not command:
        return None
    if os.sep in command:
        return command if os.path.isfile(command) and os.access(command, os.X_OK) else None
    return shutil.which(command, path=os.pathsep.join(os.get_exec_path(env)))


# ---------------------------------------------------------------------------
# /proc
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcStat:
    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int


def read_stat(pid: int) -> ProcStat | None:
    """``/proc/<pid>/stat`` 的幾個欄位；程序不在或讀不到時回 None。comm 可能含空白與括號，從最後一個 ``)`` 之後切。"""
    try:
        raw = (_PROC / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    end = raw.rfind(b")")
    fields = raw[end + 2:].split() if end >= 0 else []
    try:
        return ProcStat(pid, fields[0].decode("ascii", "replace"), int(fields[1]), int(fields[2]), int(fields[3]))
    except (IndexError, ValueError):
        return None


def _pids():
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return
    for name in entries:
        if name.isdigit():
            yield int(name)


# ---------------------------------------------------------------------------
# process group
# ---------------------------------------------------------------------------


def leader_unreaped(proc) -> bool:
    """*proc*（我們 spawn 的 asyncio Process）是否還沒被收屍（還活著或是 zombie）。

    還沒收屍的 PID 不會被系統重用，而 leader 是 session leader、不能離開自己的 process group，所以這段
    期間 PGID＝它的 PID 一定是我們建立的那一個。以 ``waitid(WNOWAIT)`` 查，不會搶走 asyncio 的收屍。
    """
    if proc.returncode is not None:
        return False
    waitid = getattr(os, "waitid", None)
    if waitid is None or not hasattr(os, "WNOWAIT"):
        return True
    try:
        waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    except OSError:
        return True
    return True


def signal_group(proc, pgid: int, sig: int) -> bool:
    """leader 還沒收屍時對整個 process group 送 *sig*；leader 已收屍就不送（之後由 :func:`sweep_group`）。"""
    if not leader_unreaped(proc):
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


@dataclass
class GroupSweep:
    live: list[ProcStat] = field(default_factory=list)
    reaped: int = 0
    signalled: bool = False


def sweep_group(pgid: int, leader_pid: int, sig: int | None) -> GroupSweep:
    """leader 收屍之後：列出 process group 裡還活著的成員、收掉過繼給本程序的 zombie 成員，必要時送 *sig*。

    只有這一次掃描看到活著的成員才 ``killpg``（同一個同步步驟、中間沒有 await）：成員還在就佔著這個
    PGID，號碼不可能已被別的 group 重用。本程序是容器的 PID 1 時，被殺的孫行程會過繼給它、成為它的
    zombie；asyncio 只收自己 spawn 的 PID，所以這裡用 ``waitpid(WNOHANG)`` 收（只收這個 group 裡、
    父程序是本程序、不是 leader 的 zombie）。非 Linux：沒有 /proc，只盡力 ``killpg`` 一次。
    """
    result = GroupSweep()
    if not LINUX:
        if sig is not None:
            try:
                os.killpg(pgid, sig)
                result.signalled = True
            except OSError:
                pass
        return result
    me = os.getpid()
    for pid in _pids():
        stat = read_stat(pid)
        if stat is None or stat.pgrp != pgid:
            continue
        if stat.state in ("Z", "X"):
            if stat.state == "Z" and stat.ppid == me and pid != leader_pid:
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:
                        result.reaped += 1
                except ChildProcessError:
                    pass
            continue
        result.live.append(stat)
    if result.live and sig is not None:
        try:
            os.killpg(pgid, sig)
            result.signalled = True
        except ProcessLookupError:
            pass
    return result


# ---------------------------------------------------------------------------
# port：spawn 前
# ---------------------------------------------------------------------------


def port_occupied(port: int) -> str | None:
    """子行程綁 ``127.0.0.1:port``／``[::1]:port`` 會不會撞到既有的 socket；會的話回原因（給 log），否則 None。

    以 ``SO_REUSEADDR`` 試綁 ``127.0.0.1``、``0.0.0.0``、``[::1]`` 與 ``[::]``（v6only）：任何 LISTEN 中的
    socket、或沒有 ``SO_REUSEADDR`` 的已綁定 socket（任一位址、含 wildcard）都會讓試綁失敗；TIME_WAIT 之類
    可重用的不會（與 uvicorn、Node、Go 的 listener 綁定規則相同）。Linux 上 wildcard 試綁已涵蓋特定位址；
    BSD／macOS 的 ``SO_REUSEADDR`` 允許 wildcard 疊在特定位址上，所以特定位址也各試一次。試綁的 socket 立刻
    關掉、沒有 listen 也沒有連線。
    """
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "0.0.0.0"),
                            (socket.AF_INET6, "::1"), (socket.AF_INET6, "::")):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # 沒有這個位址家族（例如關掉 IPv6）
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind((address, port))
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                continue
            name = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            return f"{'IPv6' if family == socket.AF_INET6 else 'IPv4'} test bind failed ({name})"
        finally:
            probe.close()
    return None


def _listening(port: int | None = None) -> list[tuple[int, int, int]] | None:
    """會接受 ``127.0.0.1`` 連線的 LISTEN socket：``[(port, inode, uid)]``；沒有 ``/proc/net/tcp`` 回 None。

    *port* 是 None 時列出所有位址的 LISTEN socket（給 :func:`admin_listener_ports`）。
    """
    found: list[tuple[int, int, int]] = []
    for name, v6 in (("tcp", False), ("tcp6", True)):
        try:
            text = (_PROC / "net" / name).read_text()
        except FileNotFoundError:
            if not v6:
                return None
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != _TCP_LISTEN:
                continue
            address, _, port_hex = fields[1].rpartition(":")
            try:
                local_port = int(port_hex, 16)
                if port is not None and (local_port != port or _address(address) not in _ACCEPTS_LOOPBACK):
                    continue
                found.append((local_port, int(fields[9]), int(fields[7])))
            except ValueError:
                continue
    return found


def _address(text: str):
    """``/proc/net/tcp*`` 的十六進位位址：每個 32 位元字以主機位元組序印出。"""
    raw = bytes.fromhex(text)
    if sys.byteorder == "little":
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return ipaddress.ip_address(raw)


def _socket_holders(inodes: set[int], pids=None) -> dict[int, set[int]]:
    """``{inode: {持有它的 PID}}``：掃 ``/proc/<pid>/fd``（讀不到的程序略過，呼叫端把找不到持有者當成失敗）。"""
    holders: dict[int, set[int]] = {inode: set() for inode in inodes}
    wanted = {f"socket:[{inode}]": inode for inode in inodes}
    for pid in (_pids() if pids is None else pids):
        fd_dir = f"/proc/{pid}/fd"
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            try:
                link = os.readlink(f"{fd_dir}/{name}")
            except OSError:
                continue
            inode = wanted.get(link)
            if inode is not None:
                holders[inode].add(pid)
    return holders


def admin_listener_ports() -> set[int]:
    """管理程序（本程序）自己的 listener port。

    Linux：本程序持有的 LISTEN socket（uvicorn 開始聽之後，以及 ``--fd``／多 worker 繼承的 socket）。
    uvicorn 是在 lifespan（預設子行程在這裡啟動）**之後**才綁 port，所以另外讀 uvicorn 命令列的
    ``--port``（沒有就看 ``UVICORN_PORT``，再沒有是 uvicorn 預設的 8000；``--fd``／``--uds`` 不算）。
    """
    ports: set[int] = set()
    if LINUX:
        listening = _listening()
        if listening:
            own = _socket_holders({inode for _, inode, _ in listening}, pids=[os.getpid()])
            ports.update(port for port, inode, _ in listening if own.get(inode))
    cli = _uvicorn_cli_port(sys.argv, os.environ)
    if cli is not None:
        ports.add(cli)
    return ports


def _uvicorn_cli_port(argv: list[str], environ) -> int | None:
    if not argv:
        return None
    program = argv[0].replace("\\", "/")
    if os.path.basename(program) != "uvicorn" and not program.endswith("uvicorn/__main__.py"):
        return None
    value = None
    for index, arg in enumerate(argv[1:], start=1):
        if arg in ("--fd", "--uds") or arg.startswith(("--fd=", "--uds=")):
            return None
        if arg == "--port" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--port="):
            value = arg.partition("=")[2]
    if value is None:
        value = environ.get("UVICORN_PORT", "8000")
    try:
        port = int(value)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


# ---------------------------------------------------------------------------
# peer：誰在聽閘道要連的 127.0.0.1:P
# ---------------------------------------------------------------------------


class Peer(enum.Enum):
    OWNED = "owned"                  # 每一個會接受連線的 LISTEN socket 都只由子行程的 session 持有
    NOT_LISTENING = "not_listening"  # 還沒有任何 LISTEN socket（子行程還在啟動，或已經不聽了）
    FOREIGN = "foreign"              # 有 socket 不是（或看不出是）子行程 session 的
    UNSUPPORTED = "unsupported"      # 沒有 /proc/net/tcp（非 Linux）


@dataclass(frozen=True)
class PeerReport:
    verdict: Peer
    listeners: int = 0
    foreign_pids: tuple[int, ...] = ()
    unattributed: int = 0  # 找不到持有者（其他使用者或其他 PID namespace 的程序）、或 socket 的 uid 不是本程序的

    def describe(self) -> str:
        return (f"{self.verdict.value}: {self.listeners} listener(s), foreign pid(s) {list(self.foreign_pids)}, "
                f"{self.unattributed} listener(s) without a visible owner in this user")


def listener_report(port: int, session: int) -> PeerReport:
    """會接受 ``127.0.0.1:port`` 連線的每一個 LISTEN socket，是否只由 session *session* 的程序持有。

    同步、只讀 /proc；呼叫端用 ``asyncio.to_thread``。找不到持有者、socket 的 uid 不是本程序的、
    持有者已經不在（讀不到 stat）都算 FOREIGN：寧可不用這個世代，也不把流量送給看不出身分的程序。
    """
    try:
        listening = _listening(port)
    except OSError:
        return PeerReport(Peer.FOREIGN)
    if listening is None:
        return PeerReport(Peer.UNSUPPORTED)
    if not listening:
        return PeerReport(Peer.NOT_LISTENING)
    holders = _socket_holders({inode for _, inode, _ in listening})
    uid = os.geteuid()
    foreign: set[int] = set()
    unattributed = 0
    for _, inode, owner_uid in listening:
        pids = holders.get(inode) or set()
        if not pids or owner_uid != uid:
            unattributed += 1
        for pid in pids:
            stat = read_stat(pid)
            if stat is None or stat.session != session:
                foreign.add(pid)
    if foreign or unattributed:
        return PeerReport(Peer.FOREIGN, len(listening), tuple(sorted(foreign)), unattributed)
    return PeerReport(Peer.OWNED, len(listening))
