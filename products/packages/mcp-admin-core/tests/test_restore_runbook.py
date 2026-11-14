"""docs/paas-config-restore-runbook.md 的程式區塊（2026-11-13 多帳號實測 X1、P3-d）。

手冊是人照著貼上執行的。這裡把區塊原樣抽出來，交給 bash 與一個只認得手冊指令的假 kubectl 執行，
設定卷用合成的 config.json（假憑證）：

- P3-d：上一次的還原 Job（或前置檢查 Job）還在時，舊寫法 ``kubectl apply`` 只印 ``unchanged``，輪詢接著讀到上一次的
  ``succeeded`` 與舊 Pod 的 ``exit=0``，看起來像成功但其實沒有執行。新區塊要先刪掉舊 Job 並等它與它的 Pod 消失、
  用 ``create`` 建立、檢查結果，而且只讀這次建立的 Job（uid）的 Pod。
- X1：還原會把帳號清單、名稱、工具政策、寫入開關、Reviewed Odoo methods 與後端連線都帶回備份當時（只有 MCP 憑證
  會重設）。步驟 1 以 ``profile-inventory.py`` 記下現況（不輸出任何憑證；後端連線只記 Odoo 的網址、資料庫與使用者），
  步驟 9 在發 token 之前比對還原後的狀態，要能列出每一種差異，處理完才印 ``RESULT: OK``。core 與各 app 經
  ``store.patch`` 寫進 mcp_server／tools 的欄位都要歸類，新增每帳號政策欄位卻沒有同步帳號清單時測試會失敗。
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = ROOT / "docs" / "paas-config-restore-runbook.md"
FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.S | re.M)
OLD_UID = "00000000-0000-4000-8000-0000000000aa"


def _blocks() -> list[tuple[str, str]]:
    return FENCE.findall(RUNBOOK.read_text(encoding="utf-8"))


def block(lang: str, *needles: str) -> str:
    found = [body for kind, body in _blocks() if kind == lang and all(n in body for n in needles)]
    assert len(found) == 1, f"手冊裡找不到（或不只一個）含 {needles} 的 {lang} 區塊"
    return found[0]


def k_line() -> str:
    first = block("bash", "DEPLOY=$($K get deploy").splitlines()[0]
    assert first.startswith("K=")
    return first


def prep() -> str:
    return block("bash", "wait_job()", "job_report()")


def step5() -> str:
    return block("bash", "restore-job.yaml", "job_report mcp-restore-inplace")


def inventory_script() -> str:
    return block("python", "SWITCHES")


# ---------------------------------------------------------------------------------------------------------------
# 假的 kubectl：狀態（Job、Pod、要回的錯誤）在 FAKE_KUBECTL_STATE；每次呼叫都記在 calls。
FAKE_KUBECTL = r'''
import json, os, re, subprocess, sys, uuid

STATE = os.environ["FAKE_KUBECTL_STATE"]
with open(STATE) as fh:
    s = json.load(fh)
argv = sys.argv[1:]
s["calls"].append(argv)


def finish(code=0, out="", err=""):
    with open(STATE, "w") as fh:
        json.dump(s, fh)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)


remote = argv[argv.index("--") + 1:] if "--" in argv else []
local = argv[:argv.index("--")] if "--" in argv else argv
VALUED = {"--context", "-n", "--namespace", "-o", "--output", "-l", "--selector", "-f", "--filename", "-c",
          "--container", "--timeout", "--request-timeout", "--cascade"}
words, opts, i = [], {}, 0
while i < len(local):
    a = local[i]
    if a.startswith("--") and "=" in a:
        key, value = a.split("=", 1)
        opts[key] = value
    elif a in VALUED:
        opts[a] = local[i + 1]
        i += 1
    elif a.startswith("-"):
        opts[a] = True
    else:
        words.append(a)
    i += 1
fmt = opts.get("-o") or opts.get("--output") or ""
verb = words[0]
target = words[1] if len(words) > 1 else ""


def render(template, job):
    values = {".metadata.uid": job["uid"], ".metadata.name": job.get("name", ""),
              ".status.succeeded": str(job.get("succeeded") or ""), ".status.failed": str(job.get("failed") or ""),
              '.status.conditions[?(@.type=="Failed")].status': job.get("cond", "")}
    def one(m):
        if m.group(1) not in values:
            finish(2, err="fake kubectl: unsupported jobpath " + m.group(1) + "\n")
        return values[m.group(1)]
    return re.sub(r"\{([^{}]*)\}", one, template)


if verb == "get" and target.startswith("job/"):
    name = target[4:]
    job = s["jobs"].get(name)
    if job is None:
        if opts.get("--ignore-not-found"):
            finish(0)
        finish(1, err='Error from server (NotFound): jobs.batch "%s" not found\n' % name)
    if fmt == "name":
        finish(0, "job.batch/%s\n" % name)
    finish(0, render(fmt[len("jsonpath="):], dict(job, name=name)))

if verb == "get" and target == "pods":
    selector = opts.get("-l") or opts.get("--selector") or ""
    name = selector.split("=", 1)[1]
    pods = [p for p in s["pods"] if p["job"] == name]
    if fmt == "name":
        finish(0, "".join("pod/%s\n" % p["name"] for p in pods))
    if "ownerReferences" in fmt:
        finish(0, "".join((p["owner"] or "") + "\n" for p in pods))
    if "exitCode" in fmt:
        finish(0, "".join("%s exit=%s\n" % (p["name"], "" if p["exit"] is None else p["exit"]) for p in pods))
    finish(2, err="fake kubectl: unsupported pods output\n")

if verb == "delete" and target.startswith("job/"):
    name = target[4:]
    job = s["jobs"].get(name)
    if job is None:
        if opts.get("--ignore-not-found"):
            finish(0)
        finish(1, err='Error from server (NotFound): jobs.batch "%s" not found\n' % name)
    if job.get("hang_delete"):
        finish(1, err="error: timed out waiting for the condition on jobs/%s\n" % name)
    del s["jobs"][name]
    if opts.get("--cascade") == "orphan" or job.get("leave_pods"):
        for p in s["pods"]:
            if p["owner"] == job["uid"]:
                p["owner"] = None
    else:
        s["pods"] = [p for p in s["pods"] if p["owner"] != job["uid"]]
    finish(0, 'job.batch "%s" deleted\n' % name)

if verb in ("create", "delete") and target in ("configmap", "configmap/mcp-restore-preflight"):
    finish(0, "configmap ok\n")

if verb in ("create", "apply") and (opts.get("-f") or opts.get("--filename")):
    source = opts.get("-f") or opts.get("--filename")
    text = sys.stdin.read() if source == "-" else open(source).read()
    name = re.search(r"^metadata:\n(?:  .*\n)*?  name: (\S+)", text, re.M).group(1)
    s["manifests"].append(text)
    if name in s["jobs"]:
        if verb == "apply":
            finish(0, "job.batch/%s unchanged\n" % name)
        finish(1, err='Error from server (AlreadyExists): error when creating "%s": jobs.batch "%s" already exists\n'
               % (source, name))
    if s.get("create_error"):
        finish(1, err=s["create_error"])
    uid = str(uuid.uuid4())
    run = s["next_run"]
    s["jobs"][name] = {"uid": uid, "succeeded": run["succeeded"], "failed": run["failed"], "cond": run["cond"]}
    s["pods"].append({"name": "%s-%s" % (name, uuid.uuid4().hex[:5]), "job": name, "owner": uid,
                      "exit": run["exit"], "log": run["log"]})
    if fmt == "jsonpath={.metadata.uid}":
        finish(0, uid)
    finish(0, "job.batch/%s created\n" % name)

if verb == "logs":
    pod = next((p for p in s["pods"] if p["name"] == target.split("/", 1)[-1]), None)
    if pod is None:
        finish(1, err='Error from server (NotFound): pods "%s" not found\n' % target)
    finish(0, pod["log"] + "\n")

if verb == "rollout" and target == "status":
    finish(0, "deployment successfully rolled out\n")

if verb == "exec" and remote[:2] == ["python3", "-"]:
    # 只在「服務 Pod」裡跑手冊的 python：-I -S 證明它只用標準函式庫
    r = subprocess.run([sys.executable, "-I", "-S", "-"] + remote[2:], input=sys.stdin.buffer.read(),
                       capture_output=True, env={"MCP_ADMIN_CONFIG": s["config"], "LC_ALL": "C.UTF-8"})
    finish(r.returncode, r.stdout.decode(), r.stderr.decode())

finish(2, err="fake kubectl: unsupported command %r\n" % argv)
'''


class FakeCluster:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = tmp_path / "fake_kubectl.py"
        fake.write_text(FAKE_KUBECTL)
        for name, body in (("kubectl", f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n'),
                           ("python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'),
                           ("sleep", "#!/bin/sh\nexit 0\n")):   # 輪詢不真的等
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.state_file = tmp_path / "kubectl-state.json"
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.state()

    def state(self, **kw) -> None:
        base = {"calls": [], "jobs": {}, "pods": [], "manifests": [], "config": "",
                "next_run": {"succeeded": 1, "failed": 0, "cond": "", "exit": 0, "log": "NEW RUN"}}
        base.update(kw)
        self.state_file.write_text(json.dumps(base))

    def run(self, script: str, **env: str) -> tuple[subprocess.CompletedProcess, dict]:
        full_env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_STATE": str(self.state_file),
                    "NS": "dev-sandbox-runbook-test", "REL": "svc-runbook-test", "HOME": str(self.tmp),
                    "LC_ALL": "C.UTF-8", **env}
        r = subprocess.run(["bash", "-c", k_line() + "\n" + script], cwd=self.work, env=full_env,
                           capture_output=True, text=True, timeout=180)
        return r, json.loads(self.state_file.read_text())


@pytest.fixture
def kube(tmp_path):
    if shutil.which("bash") is None or shutil.which("timeout") is None:
        pytest.skip("需要 bash 與 coreutils timeout")
    return FakeCluster(tmp_path)


def _old_job(name: str, log: str, **extra) -> dict:
    return {"jobs": {name: {"uid": OLD_UID, "succeeded": 1, "failed": 0, "cond": "", **extra}},
            "pods": [{"name": name + "-old00", "job": name, "owner": OLD_UID, "exit": 0, "log": log}]}


def _restore_job_yaml(kube: FakeCluster) -> None:
    job = block("yaml", "name: mcp-restore-inplace")
    for placeholder, value in (("<NS>", "dev-sandbox-runbook-test"), ("<REL>", "svc-runbook-test"),
                               ("<$PVC：服務的設定卷 PVC>", "svc-runbook-test-mcp-server-data"),
                               ("<步驟 1 的 $IMAGE：repo@sha256:<64 位 hex>，不可用 tag>",
                                "registry.example/odoo-mcp-admin@sha256:" + "0" * 64)):
        assert placeholder in job
        job = job.replace(placeholder, value)
    (kube.work / "restore-job.yaml").write_text(job)


def _call_index(state: dict, *words: str) -> list[int]:
    return [i for i, call in enumerate(state["calls"]) if all(w in call for w in words)]


# ---------------------------------------------------------------------------------------------------------------
# P3-d：步驟 5 與兩個前置檢查 Job 不可把上一次留下的同名 Job 當成這次的結果


def test_step5_reruns_instead_of_reporting_the_previous_job(kube):
    kube.state(**_old_job("mcp-restore-inplace", "OLD RESTORE RESULT"))
    _restore_job_yaml(kube)
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "OLD RESTORE RESULT" not in out and "mcp-restore-inplace-old00" not in out, out
    new_uid = s["jobs"]["mcp-restore-inplace"]["uid"]
    assert new_uid != OLD_UID
    assert f"created uid={new_uid}" in out and "job succeeded" in out and "NEW RUN" in out, out
    deletes = _call_index(s, "delete", "job/mcp-restore-inplace")
    creates = _call_index(s, "create", "-f")
    assert deletes and creates and deletes[0] < creates[0], s["calls"]
    assert not any("apply" in call for call in s["calls"]), "Job 一律 create，不用 apply"
    assert any("--cascade=foreground" in call for call in s["calls"]), "刪 Job 要連 Pod 一起等到消失"


def test_step5_stops_when_the_old_job_cannot_be_deleted(kube):
    kube.state(**_old_job("mcp-restore-inplace", "OLD RESTORE RESULT", hang_delete=True))
    _restore_job_yaml(kube)
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "STOP" in r.stderr, out
    assert "job succeeded" not in out and "OLD RESTORE RESULT" not in out, out
    assert not s["manifests"], "舊 Job 沒刪掉就不可以建立新的"


def test_step5_stops_when_old_pods_are_left_behind(kube):
    kube.state(**_old_job("mcp-restore-inplace", "OLD RESTORE RESULT", leave_pods=True))
    _restore_job_yaml(kube)
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "STOP" in r.stderr and "job succeeded" not in out and "OLD RESTORE RESULT" not in out, out
    assert not s["manifests"]


def test_step5_stops_when_create_fails(kube):
    kube.state(create_error='Error from server (Forbidden): error when creating "restore-job.yaml": jobs.batch '
                            '"mcp-restore-inplace" is forbidden: exceeded quota: audit-quota\n')
    _restore_job_yaml(kube)
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "STOP" in r.stderr and "exceeded quota" in r.stderr, out
    assert "job succeeded" not in out and "job failed" not in out and "NO POD" not in out, out
    assert not any("--request-timeout=20s" in call and "job/mcp-restore-inplace" in call
                   and any(c.startswith("jsonpath=") for c in call) for call in s["calls"]), "建立失敗就不輪詢"


def test_job_report_reads_only_pods_of_the_given_job(kube):
    new_uid = "00000000-0000-4000-8000-0000000000bb"
    kube.state(jobs={"mcp-restore-inplace": {"uid": new_uid, "succeeded": 1, "failed": 0, "cond": ""}},
               pods=[{"name": "mcp-restore-inplace-old00", "job": "mcp-restore-inplace", "owner": OLD_UID,
                      "exit": 0, "log": "OLD RESTORE RESULT"},
                     {"name": "mcp-restore-inplace-new00", "job": "mcp-restore-inplace", "owner": new_uid,
                      "exit": 0, "log": "NEW RESTORE RESULT"}])
    r, _ = kube.run(prep() + f'\njob_report mcp-restore-inplace {new_uid}; echo "rc=$?"')
    assert "rc=4" in r.stdout and "STOP" in r.stderr, r.stdout + r.stderr
    assert "RESTORE RESULT" not in r.stdout + r.stderr
    kube.state(jobs={"mcp-restore-inplace": {"uid": new_uid, "succeeded": 1, "failed": 0, "cond": ""}},
               pods=[{"name": "mcp-restore-inplace-new00", "job": "mcp-restore-inplace", "owner": new_uid,
                      "exit": 0, "log": "NEW RESTORE RESULT"}])
    r, _ = kube.run(prep() + f'\njob_report mcp-restore-inplace {new_uid}; echo "rc=$?"')
    assert "mcp-restore-inplace-new00 exit=0" in r.stdout and "NEW RESTORE RESULT" in r.stdout
    assert "rc=0" in r.stdout


def test_wait_job_stops_when_the_job_was_replaced(kube):
    kube.state(jobs={"mcp-restore-inplace": {"uid": "00000000-0000-4000-8000-0000000000cc", "succeeded": 1,
                                             "failed": 0, "cond": ""}})
    r, _ = kube.run(prep() + f'\nwait_job mcp-restore-inplace {OLD_UID} 3; echo "rc=$?"')
    assert "rc=4" in r.stdout and "STOP" in r.stderr and "job succeeded" not in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("name", ["mcp-restore-preflight-image", "mcp-restore-preflight-source"])
def test_preflight_blocks_do_not_report_a_previous_check(kube, name):
    kube.state(**_old_job(name, '{"preflight": "image_ok", "from": "OLD CHECK"}'),
               next_run={"succeeded": 1, "failed": 0, "cond": "", "exit": 0,
                         "log": '{"preflight": "image_ok", "from": "NEW CHECK"}'})
    r, s = kube.run(prep() + "\n" + block("bash", f"name: {name}"),
                    IMAGE="registry.example/odoo-mcp-admin@sha256:" + "0" * 64)
    out = r.stdout + r.stderr
    assert "OLD CHECK" not in out and "NEW CHECK" in out, out
    assert name not in s["jobs"], "檢查完要刪掉 Job"


def test_job_blocks_never_apply_jobs_and_always_pass_the_job_uid():
    for kind, body in _blocks():
        if kind != "bash":
            continue
        assert "apply -f" not in body, body[:200]
        for line in body.splitlines():
            if re.match(r"\s*(wait_job|job_report) \S", line):
                assert '"$JOB_UID"' in line, line


# ---------------------------------------------------------------------------------------------------------------
# X1：步驟 1 記下帳號清單、工具政策與寫入開關，步驟 9 比對


def _pid() -> str:
    return secrets.token_hex(12)


def _backend(pid: str) -> dict:
    """預設的後端帳號（Odoo 的網址、資料庫、使用者），依帳號 id 固定：同一個帳號每次寫卷都一樣。"""
    return {"odoo_url": f"https://odoo-{pid}.example.invalid", "odoo_db": f"db_{pid}", "odoo_username": f"user-{pid}"}


class Volume:
    """合成的設定卷（預設帳號＋額外帳號）。

    憑證（管理密碼、MCP token、generation、後端密碼、n8n bearer）都是隨機值，記在 secrets 供外洩檢查。後端帳號
    （網址、資料庫、使用者）不是憑證，帳號清單會記它（步驟 9 發 token 前比對），值由 _backend 依帳號 id 決定。
    """

    def __init__(self, root: Path):
        self.root = root
        self.secrets: list[str] = []

    def _secret(self) -> str:
        value = secrets.token_urlsafe(24)
        self.secrets.append(value)
        return value

    def _config(self, *, pid, disabled=(), ops=None, switches=None, token="active", n8n=False, port=8000,
                methods=None, backend=None) -> dict:
        """methods：Reviewed Odoo methods（mcp_server.odoo_allowed_methods），None＝沒有存過；backend：後端帳號。"""
        env = dict(switches or {})
        proxy = {"timeout": 86400}
        if n8n:
            env["AUTH_TOKEN"] = proxy["bearer_token"] = self._secret()
        server = {"command": "woow-odoo-mcp", "args": [], "port": port, "env": env, "env_prefix": ""}
        if methods is not None:
            server["odoo_allowed_methods"] = list(methods)
        return {"admin_password": self._secret(),
                "mcp_auth_token": self._secret() if token == "active" else "",
                "mcp_auth_revoked": token == "revoked",
                "mcp_auth_generation": self._secret(), "admin_session_generation": self._secret(),
                "token_history": [{"action": "auto_issue", "at": 1}] if token == "active" else [],
                "connection": {**(_backend(pid) if backend is None else backend), "odoo_password": self._secret()},
                "tools": {"disabled": list(disabled), "disabled_operations": ops or {},
                          "owner_defaults_20261025": {"applied": "fresh_install", "source": "deploy_seed"}},
                "mcp_server": server, "proxy": proxy, "admin_session_revocations": {}}

    def write(self, default: dict, extra: list[dict]) -> None:
        """default／extra 的每個 dict：id、label、以及 _config 的參數。"""
        if self.root.exists():
            shutil.rmtree(self.root)
        (self.root / "profiles").mkdir(parents=True)
        root = self._config(pid="default", **default)
        root["profile_index"] = {}
        for n, p in enumerate(extra):
            kw = {k: v for k, v in p.items() if k not in ("id", "label")}
            cfg = self._config(pid=p["id"], port=18000 + n, **kw)
            (self.root / "profiles" / p["id"]).mkdir()
            (self.root / "profiles" / p["id"] / "config.json").write_text(json.dumps(cfg))
            root["profile_index"][p["id"]] = {"label": p["label"], "port": 18000 + n}
        (self.root / "config.json").write_text(json.dumps(root))

    @property
    def config(self) -> str:
        return str(self.root / "config.json")


def _assert_no_secret(volume: Volume, text: str) -> None:
    leaked = [v for v in volume.secrets if v in text]
    assert not leaked, "帳號清單輸出了憑證或後端連線"


def _inventory(volume: Volume, cwd: Path) -> subprocess.CompletedProcess:
    script = cwd / "profile-inventory.py"
    script.write_text(inventory_script())
    return subprocess.run([sys.executable, "-I", "-S", "-", "inventory"], input=script.read_text(), text=True,
                          capture_output=True, env={"MCP_ADMIN_CONFIG": volume.config, "LC_ALL": "C.UTF-8"})


def _diff(cwd: Path, *files: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(cwd / "profile-inventory.py"), "diff", *files], cwd=cwd,
                          text=True, capture_output=True, env={"LC_ALL": "C.UTF-8"})


B, C, D, E, E2 = (_pid() for _ in range(5))
WRITES_ON = {"ODOO_MCP_ENABLE_WRITES": "1", "ODOO_MCP_ALLOW_DIRECT_WRITES": "1",
             "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "1", "MCP_CHATTER_DIRECT": "1"}
BEFORE = ({"disabled": ["fit_gap_report"], "switches": WRITES_ON},
          [{"id": B, "label": "mp-B-renamed", "switches": {**WRITES_ON, "ODOO_MCP_ENABLE_WRITES": "0"}},
           {"id": C, "label": "mp-C", "disabled": ["upgrade_risk_report"], "switches": WRITES_ON,
            "ops": {"execute_approved_write": ["unlink"]}},
           {"id": E, "label": "mp-E", "disabled": ["fit_gap_report", "upgrade_risk_report"], "switches": WRITES_ON,
            "n8n": True}])
RESTORED = ({"disabled": ["fit_gap_report"], "switches": WRITES_ON, "token": "revoked"},
            [{"id": B, "label": "mp-B", "switches": WRITES_ON, "token": "revoked"},
             {"id": C, "label": "mp-C", "switches": WRITES_ON, "token": "revoked"},
             {"id": D, "label": "mp-D", "disabled": ["data_quality_report"], "switches": WRITES_ON,
              "token": "revoked"}])
RECONCILED = ({"disabled": ["fit_gap_report"], "switches": WRITES_ON, "token": "revoked"},
              [{"id": B, "label": "mp-B-renamed", "switches": {**WRITES_ON, "ODOO_MCP_ENABLE_WRITES": "0"},
                "token": "revoked"},
               {"id": C, "label": "mp-C", "disabled": ["upgrade_risk_report"], "switches": WRITES_ON,
                "ops": {"execute_approved_write": ["unlink"]}, "token": "revoked"},
               # 核對時重建：新的 id，建立時自動有 token（active）；Connection 頁照步驟 1 的後端帳號填
               {"id": E2, "label": "mp-E", "disabled": ["fit_gap_report", "upgrade_risk_report"],
                "switches": WRITES_ON, "n8n": True, "backend": _backend(E)}])


def test_inventory_lists_profiles_and_policies_without_credentials(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    r = _inventory(volume, tmp_path)
    assert r.returncode == 0, r.stderr
    _assert_no_secret(volume, r.stdout + r.stderr)
    data = json.loads(r.stdout)
    rows = {p["id"]: p for p in data["profiles"]}
    assert list(rows) == ["default", B, C, E]
    assert rows["default"]["label"] == "Default" and rows["default"]["token"] == "active"
    assert rows[B]["label"] == "mp-B-renamed" and rows[B]["switches"]["ODOO_MCP_ENABLE_WRITES"] == "0"
    assert rows[C]["tools_disabled"] == ["upgrade_risk_report"]
    assert rows[C]["tools_disabled_operations"] == {"execute_approved_write": ["unlink"]}
    assert rows[E]["tools_disabled"] == ["fit_gap_report", "upgrade_risk_report"]
    assert "AUTH_TOKEN" not in json.dumps(rows[E]), "n8n 子程序 bearer 的鍵也不讀出"
    assert rows[B]["backend"] == _backend(B), "後端帳號只記網址、資料庫、使用者（不含密碼）"
    assert len(r.stderr.strip().splitlines()) == 4, "每個帳號一行摘要"


def test_inventory_marks_unreadable_profiles_and_missing_config(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    (volume.root / "profiles" / C / "config.json").write_text("{broken")
    r = _inventory(volume, tmp_path)
    assert r.returncode == 1
    rows = {p["id"]: p for p in json.loads(r.stdout)["profiles"]}
    assert rows[C]["error"] == "unreadable" and "error" not in rows[B]
    _assert_no_secret(volume, r.stdout + r.stderr)
    (volume.root / "config.json").unlink()
    r = _inventory(volume, tmp_path)
    assert r.returncode == 2 and "STOP" in r.stderr and r.stdout == ""


def test_inventory_escapes_labels_on_the_terminal(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write({}, [{"id": B, "label": "mp-\x1b[31mB\u202e"}, {"id": C, "label": "業務 帳號"}])
    r = _inventory(volume, tmp_path)
    assert r.returncode == 0
    assert "\x1b" not in r.stdout + r.stderr and "\u202e" not in r.stdout + r.stderr
    assert "業務 帳號" in r.stderr, "可列印的中文名稱照原樣顯示"
    assert {p["label"] for p in json.loads(r.stdout)["profiles"]} == {"Default", "mp-\x1b[31mB\u202e", "業務 帳號"}


def _tags(output: str) -> list[str]:
    """差異行的標記；[已重建] 只是說明，不算差異。"""
    return sorted(tag for tag in re.findall(r"^\[(\S+)\] ", output, re.M) if tag != "已重建")


def _snapshot(volume: Volume, cwd: Path, name: str) -> str:
    r = _inventory(volume, cwd)
    assert r.returncode == 0, r.stderr
    (cwd / name).write_text(r.stdout)
    return name


def test_diff_lists_every_kind_of_drift_after_the_restore(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write(*RESTORED)
    restored = _snapshot(volume, tmp_path, "profiles-restored.json")
    r = _diff(tmp_path, before, restored)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _tags(r.stdout) == sorted(["名稱", "開關", "工具", "操作", "刪除", "重建"]), r.stdout
    assert "RESULT: 6 項差異" in r.stdout
    assert re.search(rf"^\[刪除\] 'mp-D'（id {D}）", r.stdout, re.M)
    assert re.search(rf"^\[重建\] 'mp-E'（id {E}）.*'fit_gap_report', 'upgrade_risk_report'", r.stdout, re.M)
    _assert_no_secret(volume, r.stdout + r.stderr)


def test_diff_accepts_recreated_profiles_and_prints_ok(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write(*RESTORED)
    restored = _snapshot(volume, tmp_path, "profiles-restored.json")
    volume.write(*RECONCILED)
    now = _snapshot(volume, tmp_path, "profiles-now.json")
    r = _diff(tmp_path, before, now, restored)
    assert r.returncode == 0, r.stdout
    assert r.stdout.strip().splitlines()[-1].startswith("RESULT: OK")
    assert re.search(rf"'mp-E'（id {E}）.*{E2}", r.stdout), "列出哪個帳號是核對時重建的"


def test_diff_keeps_flagging_what_is_still_different(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write(*RESTORED)
    restored = _snapshot(volume, tmp_path, "profiles-restored.json")
    default, extra = RECONCILED
    # 重建的帳號政策沒設好、還原帶回來的 D 還在、多建了一個沒在步驟 1 的帳號
    wrong = [dict(p, disabled=["fit_gap_report"]) if p["id"] == E2 else p for p in extra]
    wrong += [RESTORED[1][2], {"id": _pid(), "label": "extra", "switches": WRITES_ON}]
    volume.write(default, wrong)
    now = _snapshot(volume, tmp_path, "profiles-now.json")
    r = _diff(tmp_path, before, now, restored)
    assert r.returncode == 1
    assert _tags(r.stdout) == sorted(["工具", "刪除", "多出"]), r.stdout


def test_diff_does_not_take_a_restored_profile_for_a_recreated_one(tmp_path):
    """備份後刪掉 D、又新增同名的 E：還原帶回來的 D 不能當成重建的 E（它帶著備份裡的後端連線）。"""
    volume = Volume(tmp_path / "data")
    volume.write({}, [{"id": E, "label": "mp-D"}])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"token": "revoked"}, [{"id": D, "label": "mp-D", "token": "revoked"}])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json")
    r = _diff(tmp_path, before, restored, restored)
    assert r.returncode == 1
    assert _tags(r.stdout) == ["刪除", "重建"], r.stdout


def test_diff_flags_restored_profiles_whose_token_is_not_revoked(tmp_path):
    """還原後、發 token 之前，還原回來的每個帳號都應該是已撤銷；不是就代表步驟 5 其實沒有執行（P3-d）。"""
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    r = _diff(tmp_path, before, before)
    assert r.returncode == 1
    assert _tags(r.stdout) == ["token"] * 4, r.stdout


def test_diff_refuses_missing_files(tmp_path):
    (tmp_path / "profile-inventory.py").write_text(inventory_script())
    r = _diff(tmp_path, "profiles-before.json", "profiles-now.json")
    assert r.returncode == 2 and "STOP" in r.stdout + r.stderr


def test_step1_and_step9_blocks_run_literally(kube):
    """手冊的步驟 1、9 區塊原樣執行：記錄、比對、核對後 RESULT: OK；還原後不可覆寫步驟 1 的紀錄。"""
    volume = Volume(kube.tmp / "data")
    volume.write(*BEFORE)
    (kube.work / "profile-inventory.py").write_text(inventory_script())
    kube.state(config=volume.config)
    shell = 'DEPLOY=svc-runbook-test-mcp-server\nCONTAINER=mcp-server\n'
    step1 = block("bash", "> profiles-before.json.tmp")
    r, _ = kube.run(shell + step1)
    assert r.returncode == 0 and "STOP" not in r.stderr, r.stdout + r.stderr
    before_text = (kube.work / "profiles-before.json").read_text()
    assert [p["id"] for p in json.loads(before_text)["profiles"]] == ["default", B, C, E]
    _assert_no_secret(volume, r.stdout + r.stderr + before_text)

    volume.write(*RESTORED)
    check = block("bash", "> profiles-restored.json.tmp")
    r, _ = kube.run(shell + check)
    assert "RESULT: 6 項差異" in r.stdout, r.stdout + r.stderr
    r, _ = kube.run(shell + step1)
    assert "STOP" in r.stderr and (kube.work / "profiles-before.json").read_text() == before_text

    volume.write(*RECONCILED)
    final = block("bash", "> profiles-now.json")
    r, _ = kube.run(shell + final)
    assert r.stdout.strip().splitlines()[-1].startswith("RESULT: OK"), r.stdout + r.stderr
    _assert_no_secret(volume, r.stdout + r.stderr + "".join(p.read_text() for p in kube.work.glob("*.json")))


# ---------------------------------------------------------------------------------------------------------------
# X1 審查（2026-11-13）：Settings 頁的 Reviewed Odoo methods（mcp_server.odoo_allowed_methods）與每個帳號的後端帳號
# 也會被還原帶回備份當時。帳號清單要記，步驟 9 要在發 token 之前比對出來。


def test_inventory_records_reviewed_odoo_methods(tmp_path):
    """沒有存過＝null（子程序沿用容器環境的清單），存過空清單＝[]（不允許任何方法），存過的清單排序後記錄。"""
    volume = Volume(tmp_path / "data")
    volume.write({}, [{"id": B, "label": "mp-B", "methods": []},
                      {"id": C, "label": "mp-C", "methods": ["sale.order.action_confirm", "account.move.action_post"]}])
    r = _inventory(volume, tmp_path)
    assert r.returncode == 0, r.stderr
    rows = {p["id"]: p for p in json.loads(r.stdout)["profiles"]}
    assert rows["default"]["odoo_allowed_methods"] is None
    assert rows[B]["odoo_allowed_methods"] == []
    assert rows[C]["odoo_allowed_methods"] == ["account.move.action_post", "sale.order.action_confirm"]
    assert "account.move.action_post" in r.stderr, "畫面上的摘要也列出方法清單"
    _assert_no_secret(volume, r.stdout + r.stderr)


def test_diff_flags_a_method_list_widened_by_the_restore(tmp_path):
    """審查重現：Allow Unreviewed Methods 關閉，步驟 1 的清單只有 sale.order.action_confirm；還原帶回備份當時多了
    account.move.action_post 的清單，token 都是 revoked。修正前比對印 RESULT: OK、exit 0，照手冊就會發 token。"""
    volume = Volume(tmp_path / "data")
    switches = {**WRITES_ON, "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "0"}
    tight, wide = ["sale.order.action_confirm"], ["sale.order.action_confirm", "account.move.action_post"]
    volume.write({"switches": switches, "methods": tight},
                 [{"id": B, "label": "mp-B", "switches": switches, "methods": tight}])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"switches": switches, "methods": wide, "token": "revoked"},
                 [{"id": B, "label": "mp-B", "switches": switches, "methods": wide, "token": "revoked"}])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json")
    r = _diff(tmp_path, before, restored)
    assert r.returncode == 1 and "RESULT: OK" not in r.stdout, r.stdout
    assert _tags(r.stdout) == ["方法", "方法"], r.stdout
    assert re.search(rf"^\[方法\] 'mp-B'（id {B}）：步驟 1 是 \['sale.order.action_confirm'\]，現在是 "
                     r"\['account.move.action_post', 'sale.order.action_confirm'\]——到 Settings 頁的 Reviewed Odoo methods",
                     r.stdout, re.M), r.stdout


@pytest.mark.parametrize(("step1", "now"), [(None, []), ([], None), (None, ["sale.order.action_confirm"])])
def test_diff_tells_an_unsaved_method_list_from_an_empty_one(tmp_path, step1, now):
    """沒有存過（沿用容器環境的清單）與存過空清單（不允許任何方法）是不同的政策。"""
    volume = Volume(tmp_path / "data")
    volume.write({"methods": step1}, [])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"methods": now, "token": "revoked"}, [])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json")
    r = _diff(tmp_path, before, restored)
    assert r.returncode == 1 and _tags(r.stdout) == ["方法"], r.stdout


def test_diff_flags_a_backend_account_switched_back_by_the_restore(tmp_path):
    """備份之後 B 改用受限的 Odoo 使用者：還原把連線（連同密碼）換回備份當時的使用者。發 token 之前就要看得出來，
    而且不印出任何密碼（審查 must-fix 2）。"""
    volume = Volume(tmp_path / "data")
    volume.write({}, [{"id": B, "label": "mp-B", "backend": {**_backend(B), "odoo_username": "mp-b-restricted"}}])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"token": "revoked"}, [{"id": B, "label": "mp-B", "token": "revoked"}])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json")
    r = _diff(tmp_path, before, restored)
    assert r.returncode == 1 and _tags(r.stdout) == ["後端"], r.stdout
    assert re.search(rf"^\[後端\] 'mp-B'（id {B}）：odoo_username 步驟 1 是 'mp-b-restricted'，現在是 'user-{B}'"
                     "——到 Connection 頁", r.stdout, re.M), r.stdout
    _assert_no_secret(volume, r.stdout + r.stderr + (tmp_path / before).read_text() + (tmp_path / restored).read_text())


def test_inventory_does_not_record_credentials_inside_backend_urls(tmp_path):
    """Odoo 的網址存檔時不過 SSRF 檢查，可能帶帳密或 query：帳號清單只記 scheme://host:port/path。"""
    volume = Volume(tmp_path / "data")
    url = f"https://mp-admin:{volume._secret()}@odoo.example.invalid:8069/odoo?api_key={volume._secret()}#frag"
    slash = f"https://mp-admin:pa/ss-{volume._secret()}@odoo.example.invalid"   # 密碼裡有沒編碼的 /
    volume.write({"backend": {**_backend("default"), "odoo_url": slash}},
                 [{"id": B, "label": "mp-B", "backend": {**_backend(B), "odoo_url": url}}])
    r = _inventory(volume, tmp_path)
    assert r.returncode == 0, r.stderr
    _assert_no_secret(volume, r.stdout + r.stderr)
    assert "mp-admin" not in r.stdout + r.stderr and "frag" not in r.stdout + r.stderr
    rows = {p["id"]: p for p in json.loads(r.stdout)["profiles"]}
    assert rows[B]["backend"]["odoo_url"] == "https://***@odoo.example.invalid:8069/odoo?***"
    assert rows["default"]["backend"]["odoo_url"] == "https://***@odoo.example.invalid"
    assert rows[B]["backend"]["odoo_db"] == _backend(B)["odoo_db"], "資料庫與使用者照原樣記"


def test_recreated_profile_is_checked_against_every_recorded_field(tmp_path):
    """重建的帳號複製預設帳號當下的 mcp_server 與 tools（profiles.py 的 create），連線是空的。[重建] 要列出步驟 1 的
    每一項，重建後也逐項比對（修正前預設帳號比較寬的方法清單會被靜默繼承，[重建] 列也沒有寫）。"""
    volume = Volume(tmp_path / "data")
    wide, tight = ["account.move.action_post", "sale.order.action_confirm"], ["sale.order.action_confirm"]
    step1 = {"id": E, "label": "mp-E", "disabled": ["fit_gap_report"], "ops": {"execute_approved_write": ["unlink"]},
             "switches": {**WRITES_ON, "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "0"}, "methods": tight}
    volume.write({"switches": WRITES_ON, "methods": wide}, [step1])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"switches": WRITES_ON, "methods": wide, "token": "revoked"}, [])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json")
    r = _diff(tmp_path, before, restored)
    line = next(x for x in r.stdout.splitlines() if x.startswith("[重建]"))
    recorded = next(p for p in json.loads((tmp_path / before).read_text())["profiles"] if p["id"] == E)
    for field in [*POLICY_FIELDS.values(), "backend"]:
        assert repr(recorded[field]) in line, (field, line)

    # 剛重建：新的 id，政策複製預設帳號（寬的方法清單、沒有停用工具與操作、預設帳號的開關），連線是空的
    copied = {"id": E2, "label": "mp-E", "switches": WRITES_ON, "methods": wide,
              "backend": dict.fromkeys(_backend(E), "")}
    volume.write({"switches": WRITES_ON, "methods": wide, "token": "revoked"}, [copied])
    r = _diff(tmp_path, before, _snapshot(volume, tmp_path, "profiles-now.json"), restored)
    assert r.returncode == 1
    assert _tags(r.stdout) == sorted(["工具", "操作", "開關", "方法", "後端", "後端", "後端"]), r.stdout

    # 照 [重建] 列設定好政策、在 Connection 頁填步驟 1 的後端帳號
    volume.write({"switches": WRITES_ON, "methods": wide, "token": "revoked"},
                 [dict(step1, id=E2, backend=_backend(E))])
    r = _diff(tmp_path, before, _snapshot(volume, tmp_path, "profiles-now.json"), restored)
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1].startswith("RESULT: OK"), r.stdout
    _assert_no_secret(volume, r.stdout + r.stderr)


def _apps() -> list[str]:
    return [app.name for app in sorted((ROOT / "apps").iterdir())
            if (app / "pyproject.toml").exists() and app.name != "odoo-manage"]


def _launch_policies() -> dict:
    return {app: importlib.import_module(app.replace("-", "_") + "_mcp_admin.main").LAUNCH_POLICY for app in _apps()}


def _script_namespace() -> dict:
    """手冊的 profile-inventory.py 載入成模組（不執行 __main__）。"""
    namespace = {"__name__": "profile_inventory"}
    exec(compile(inventory_script(), "profile-inventory.py", "exec"), namespace)
    return namespace


def test_switch_list_matches_every_product_policy():
    """SWITCHES＝各 app Settings 頁可改的 mcp_server.env 開關；其他 env 鍵（如 n8n 的 AUTH_TOKEN）是憑證，不可讀出。"""
    switches = _script_namespace()["SWITCHES"]
    editable, other = set(), set()
    for policy in _launch_policies().values():
        editable |= set(policy.editable_env_keys)
        other |= set(policy.env_keys) - set(policy.editable_env_keys)
    assert set(switches) == editable and len(switches) == len(editable)
    assert "AUTH_TOKEN" in other and not other & set(switches)


def test_backend_account_fields_match_every_product_policy():
    """IDENTITY＝各 app 換了就算換後端帳號的連線欄位（LaunchPolicy.identity_connection_keys）裡不是秘密的鍵。
    密碼、API key 這類欄位一律不讀出。"""
    from mcp_admin_core.routers.settings import _is_secret_key

    declared = set().union(*(policy.identity_connection_keys for policy in _launch_policies().values()))
    identity = _script_namespace()["IDENTITY"]
    assert identity and len(set(identity)) == len(identity)
    assert set(identity) == {key for key in declared if not _is_secret_key(key)}
    assert not any(_is_secret_key(key) for key in identity)


SECTIONS = ("mcp_server", "tools")
# core 與各 app 經 store.patch／put 寫進 mcp_server、tools 的每一個鍵都要在下面歸類。restore guard 只重設憑證，這兩個
# 區段的其他欄位都會被還原帶回備份當時。
# 每帳號的政策 → profile-inventory.py 記錄的欄位（步驟 9 逐項比對、[重建] 列出）：
POLICY_FIELDS = {
    ("tools", "disabled"): "tools_disabled",                                # Tools 頁
    ("tools", "disabled_operations"): "tools_disabled_operations",          # Permissions 頁
    ("mcp_server", "env"): "switches",                                      # Settings 頁的開關（只有 editable_env_keys）
    ("mcp_server", "odoo_allowed_methods"): "odoo_allowed_methods",         # Settings 頁的 Reviewed Odoo methods
}
# 不是每帳號政策的鍵與原因：
NOT_POLICY = {
    ("mcp_server", "env_prefix"): "部署決定的連線注入前綴，管理頁不能改（LOCKED_MCP_SERVER_KEYS）",
    ("mcp_server", "command"): "子程序由 LaunchPolicy 固定，config.json 的值不會執行（opendesign 舊 seed 遷移）",
    ("mcp_server", "args"): "同 command",
    ("mcp_server", "port"): "子程序 port，建帳號時分配，不是權限",
    ("tools", "nextcloud_policy_seeded"): "Nextcloud 一次性 seed 標記；它補上的停用工具記在 tools.disabled",
}
# 第二個參數不是 dict 常值（看不出寫哪些鍵）的寫入：寫明會寫的鍵（也要在上面兩張表裡）。
OPAQUE_WRITES = {
    # PUT /api/settings/tools 的整包 body（管理頁不用這條，Tools、Permissions 頁走 /api/tools）：read_policy 只認這兩個政策鍵
    ("packages/mcp-admin-core/mcp_admin_core/routers/settings.py", "tools"): {"disabled", "disabled_operations"},
    # 啟動時補 LITELLM_MCP_READONLY 與 env_prefix
    ("apps/litellm/litellm_mcp_admin/seed_defaults.py", "mcp_server"): {"env", "env_prefix"},
}


def _section_writes() -> tuple[dict, set]:
    """core 與各 app 以 ``.patch(區段, …)``／``.put(區段, …)`` 寫進 mcp_server、tools 的鍵。

    回傳 ({(區段, 鍵): {檔案}}, {(檔案, 區段)})；第二個是第二個參數不是 dict 常值的呼叫。鍵是模組層級的字串常數
    （例如 Odoo allowed_methods 的 ``FIELD``）時換成它的值。
    """
    files = sorted((ROOT / "packages" / "mcp-admin-core" / "mcp_admin_core").rglob("*.py"))
    for app in _apps():
        files += sorted((ROOT / "apps" / app / (app.replace("-", "_") + "_mcp_admin")).rglob("*.py"))
    writes, opaque = {}, set()
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                 and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                 for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("patch", "put") and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value in SECTIONS):
                continue
            section, value = node.args[0].value, node.args[1]
            if not isinstance(value, ast.Dict):
                opaque.add((rel, section))
                continue
            for key in value.keys:
                if isinstance(key, ast.Name) and key.id in names:
                    key = ast.Constant(names[key.id])
                assert isinstance(key, ast.Constant) and isinstance(key.value, str), f"{rel}:{node.lineno} 看不出寫入的鍵"
                writes.setdefault((section, key.value), set()).add(rel)
    return writes, opaque


def test_every_policy_field_written_by_the_apps_is_recorded():
    """2026-11-13 審查：Reviewed Odoo methods 存在 mcp_server.odoo_allowed_methods，舊版帳號清單沒記，還原放寬了清單也印
    RESULT: OK。之後新增每帳號政策欄位卻沒有同步 profile-inventory.py 時，這個測試會失敗。"""
    writes, opaque = _section_writes()
    assert ("mcp_server", "odoo_allowed_methods") in writes, "掃描要找得到 Odoo 的 store.patch('mcp_server', {FIELD: …})"
    unknown = {key: sorted(files) for key, files in writes.items() if key not in POLICY_FIELDS and key not in NOT_POLICY}
    assert not unknown, ("新的 tools／mcp_server 欄位會被還原帶回備份當時：是每帳號政策就加進手冊 profile-inventory.py 的 "
                         f"policy() 與 FIELDS、步驟 9 的表，再加進 POLICY_FIELDS；不是就寫進 NOT_POLICY 並寫明原因：{unknown}")
    assert opaque == set(OPAQUE_WRITES), f"看不出鍵的寫入要列進 OPAQUE_WRITES，寫明會寫哪些鍵：{sorted(opaque)}"
    declared = set(writes) | {(section, key) for (_path, section), keys in OPAQUE_WRITES.items() for key in keys}
    assert declared == set(POLICY_FIELDS) | set(NOT_POLICY), "表裡不要留沒有程式寫入的項目"
    namespace = _script_namespace()
    recorded, compared = set(namespace["policy"]({})), {field for field, _tag, _where in namespace["FIELDS"]}
    for field in POLICY_FIELDS.values():
        assert field in recorded and field in compared, f"profile-inventory.py 沒有記錄或比對 {field}"
