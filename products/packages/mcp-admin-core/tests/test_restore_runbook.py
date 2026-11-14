"""docs/paas-config-restore-runbook.md 的程式區塊（2026-11-13 多帳號實測 X1、P3-d 與上線後的後續修正）。

手冊是人照著貼上執行的。這裡把區塊原樣抽出來，交給 bash 與一個只認得手冊指令的假 kubectl 執行，
設定卷用合成的 config.json（假憑證）：

- P3-d：上一次的還原 Job（或前置檢查 Job）還在時，舊寫法 ``kubectl apply`` 只印 ``unchanged``，輪詢接著讀到上一次的
  ``succeeded`` 與舊 Pod 的 ``exit=0``，看起來像成功但其實沒有執行。新區塊要先刪掉舊 Job 並等它與它的 Pod 消失、
  用 ``create`` 建立、檢查結果，而且只讀這次建立的 Job（uid）的 Pod。
- 名稱（上線前重測 P3）：Job 與 ConfigMap 的名稱帶上 ``REL``，同一個 namespace 另一台服務的還原或步驟 13 不會刪到
  這次的；名稱超過 63 字元或 REL 有 a-z、0-9、- 以外的字元時改用雜湊，仍是合法而且不重複的名稱。
- ``wait_job``：Job 被刪掉（NotFound）或換成別的 uid 時立刻 STOP，不再和 API 暫時失敗一樣輪詢約 31 分鐘。
- X1：還原會把帳號清單、名稱、工具政策、寫入開關、Reviewed Odoo methods 與後端連線都帶回備份當時（只有 MCP 憑證
  會重設）。步驟 1 以 ``profile-inventory.py`` 記下現況（不輸出任何憑證；後端連線只記各產品不是秘密的網址、資料庫與
  使用者名稱），步驟 9 在發 token 之前比對還原後的狀態，要能列出每一種差異，處理完才印 ``RESULT: OK``。還原後的紀錄
  綁定步驟 5 的還原 Job uid，上一次還原留下的紀錄不會被拿來比對。core 與各 app 寫進 mcp_server／tools 的欄位都要歸類，
  看不出鍵的寫入逐一登記，新增每帳號政策欄位或沒登記的寫入時測試會失敗。
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
OTHER_UID = "00000000-0000-4000-8000-0000000000dd"
DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")


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


def names() -> str:
    """步驟 1 第一個區塊裡設定 Job 與 ConfigMap 名稱的那幾行（rname 函式到印出名稱的 echo）。"""
    lines = block("bash", "DEPLOY=$($K get deploy").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("rname()"))
    end = next(i for i, line in enumerate(lines) if line.startswith('echo "還原 Job'))
    assert start < end
    return "\n".join(lines[start:end + 1])


def prep() -> str:
    return block("bash", "wait_job()", "job_report()")


def step5() -> str:
    return block("bash", "restore-job.yaml", 'job_report "$RJOB"')


def step13() -> str:
    return block("bash", "delete pvc/${REL}-restore-source")


def inventory_script() -> str:
    return block("python", "SWITCHES")


# ---------------------------------------------------------------------------------------------------------------
# 假的 kubectl：狀態（Job、Pod、ConfigMap、要回的錯誤）在 FAKE_KUBECTL_STATE；每次呼叫都記在 calls。
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


def drop_job(name, orphan=False):
    job = s["jobs"].pop(name)
    if orphan or job.get("leave_pods"):
        for p in s["pods"]:
            if p["owner"] == job["uid"]:
                p["owner"] = None
    else:
        s["pods"] = [p for p in s["pods"] if p["owner"] != job["uid"]]


if verb == "get" and target.startswith("job/"):
    name = target[4:]
    job = s["jobs"].get(name)
    if job is not None and fmt.startswith("jsonpath=") and job.get("flaky"):   # API 暫時失敗
        job["flaky"] -= 1
        finish(1, err="Unable to connect to the server: net/http: TLS handshake timeout\n")
    if job is not None and fmt.startswith("jsonpath=") and job.get("vanish"):   # 輪詢時已經被別人刪掉
        drop_job(name)
        job = None
    if job is None:
        if opts.get("--ignore-not-found"):
            finish(0)
        finish(1, err='Error from server (NotFound): jobs.batch "%s" not found\n' % name)
    if fmt == "name":
        finish(0, "job.batch/%s\n" % name)
    finish(0, render(fmt[len("jsonpath="):], dict(job, name=name)))

if verb == "get" and target == "pods":
    if s.get("pods_error"):
        finish(1, err="Unable to connect to the server: net/http: TLS handshake timeout\n")
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
    drop_job(name, orphan=opts.get("--cascade") == "orphan")
    finish(0, 'job.batch "%s" deleted\n' % name)

if verb in ("create", "delete") and (target == "configmap" or target.startswith("configmap/")):
    name = target.split("/", 1)[1] if "/" in target else (words[2] if len(words) > 2 else "")
    if not name:
        finish(1, err="error: resource name may not be empty\n")
    if verb == "create":
        if name in s["configmaps"]:
            finish(1, err='Error from server (AlreadyExists): configmaps "%s" already exists\n' % name)
        s["configmaps"].append(name)
        finish(0, "configmap/%s created\n" % name)
    if name not in s["configmaps"]:
        if opts.get("--ignore-not-found"):
            finish(0)
        finish(1, err='Error from server (NotFound): configmaps "%s" not found\n' % name)
    s["configmaps"].remove(name)
    finish(0, 'configmap "%s" deleted\n' % name)

if verb == "delete" and target.startswith("pvc/"):
    s["deleted_pvcs"].append(target[4:])
    finish(0, 'persistentvolumeclaim "%s" deleted\n' % target[4:])

if verb in ("create", "apply") and (opts.get("-f") or opts.get("--filename")):
    source = opts.get("-f") or opts.get("--filename")
    text = sys.stdin.read() if source == "-" else open(source).read()
    name = re.search(r"^metadata:\n(?:  .*\n)*?  name: (\S+)", text, re.M).group(1)
    if opts.get("--dry-run"):
        s["dry_runs"].append(name)
        finish(0, name if fmt == "jsonpath={.metadata.name}" else "job.batch/%s created (dry run)\n" % name)
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
    s["jobs"][name] = {"uid": uid, "succeeded": run["succeeded"], "failed": run["failed"], "cond": run["cond"],
                       **{k: run[k] for k in ("vanish", "flaky") if k in run}}
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
        base = {"calls": [], "jobs": {}, "pods": [], "manifests": [], "configmaps": [], "deleted_pvcs": [],
                "dry_runs": [], "config": "",
                "next_run": {"succeeded": 1, "failed": 0, "cond": "", "exit": 0, "log": "NEW RUN"}}
        base.update(kw)
        self.state_file.write_text(json.dumps(base))

    def run(self, script: str, **env: str) -> tuple[subprocess.CompletedProcess, dict]:
        """步驟 1 第一個區塊的 ``K=`` 與名稱那幾行，加上 *script*，交給 bash。"""
        full_env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_STATE": str(self.state_file),
                    "NS": "dev-sandbox-runbook-test", "REL": "svc-runbook-test", "HOME": str(self.tmp),
                    "LC_ALL": "C.UTF-8", **env}
        r = subprocess.run(["bash", "-c", k_line() + "\n" + names() + "\n" + script], cwd=self.work, env=full_env,
                           capture_output=True, text=True, timeout=180)
        return r, json.loads(self.state_file.read_text())

    def names(self, rel: str = "svc-runbook-test") -> dict:
        r, _ = self.run('printf "\\n%s|%s|%s|%s" "$RJOB" "$PJOB_IMAGE" "$PJOB_SOURCE" "$PCM"', REL=rel)
        assert r.returncode == 0 and "STOP" not in r.stderr, r.stdout + r.stderr
        return dict(zip(("restore", "image", "source", "configmap"), r.stdout.splitlines()[-1].split("|")))


@pytest.fixture
def kube(tmp_path):
    if shutil.which("bash") is None or shutil.which("timeout") is None:
        pytest.skip("需要 bash 與 coreutils timeout")
    return FakeCluster(tmp_path)


def _job(name: str, uid: str, log: str, **extra) -> dict:
    return {"jobs": {name: {"uid": uid, "succeeded": 1, "failed": 0, "cond": "", **extra}},
            "pods": [{"name": name + "-old00", "job": name, "owner": uid, "exit": 0, "log": log}]}


def _old_job(name: str, log: str, **extra) -> dict:
    return _job(name, OLD_UID, log, **extra)


def _restore_job_yaml(kube: FakeCluster, name: str | None = None) -> str:
    job = block("yaml", "kind: Job", "mcp_admin_core.restore_inplace")
    name = name or kube.names()["restore"]
    for placeholder, value in (("<$RJOB：步驟 1 印出的還原 Job 名稱>", name), ("<NS>", "dev-sandbox-runbook-test"),
                               ("<REL>", "svc-runbook-test"),
                               ("<$PVC：服務的設定卷 PVC>", "svc-runbook-test-mcp-server-data"),
                               ("<步驟 1 的 $IMAGE：repo@sha256:<64 位 hex>，不可用 tag>",
                                "registry.example/odoo-mcp-admin@sha256:" + "0" * 64)):
        assert placeholder in job
        job = job.replace(placeholder, value)
    (kube.work / "restore-job.yaml").write_text(job)
    return name


def _call_index(state: dict, *words: str) -> list[int]:
    return [i for i, call in enumerate(state["calls"]) if all(w in call for w in words)]


def _polls(state: dict, job: str) -> list[list[str]]:
    return [c for c in state["calls"] if "get" in c and f"job/{job}" in c and any(x.startswith("jsonpath=") for x in c)]


# ---------------------------------------------------------------------------------------------------------------
# P3-d：步驟 5 與兩個前置檢查 Job 不可把上一次留下的同名 Job 當成這次的結果


def test_step5_reruns_instead_of_reporting_the_previous_job(kube):
    rjob = _restore_job_yaml(kube)
    kube.state(**_old_job(rjob, "OLD RESTORE RESULT"))
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "OLD RESTORE RESULT" not in out and f"{rjob}-old00" not in out, out
    new_uid = s["jobs"][rjob]["uid"]
    assert new_uid != OLD_UID
    assert f"created uid={new_uid}" in out and "job succeeded" in out and "NEW RUN" in out, out
    deletes = _call_index(s, "delete", f"job/{rjob}")
    creates = [i for i in _call_index(s, "create", "-f") if not any(c.startswith("--dry-run") for c in s["calls"][i])]
    assert deletes and creates and deletes[0] < creates[0], s["calls"]
    assert not any("apply" in call for call in s["calls"]), "Job 一律 create，不用 apply"
    assert any("--cascade=foreground" in call for call in s["calls"]), "刪 Job 要連 Pod 一起等到消失"
    assert (kube.work / "restore-job.uid").read_text().strip() == new_uid, "步驟 9 用這次還原 Job 的 uid"


def test_step5_stops_when_the_old_job_cannot_be_deleted(kube):
    rjob = _restore_job_yaml(kube)
    kube.state(**_old_job(rjob, "OLD RESTORE RESULT", hang_delete=True))
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "STOP" in r.stderr, out
    assert "job succeeded" not in out and "OLD RESTORE RESULT" not in out, out
    assert not s["manifests"], "舊 Job 沒刪掉就不可以建立新的"
    assert not (kube.work / "restore-job.uid").exists()


def test_step5_stops_when_old_pods_are_left_behind(kube):
    rjob = _restore_job_yaml(kube)
    kube.state(**_old_job(rjob, "OLD RESTORE RESULT", leave_pods=True))
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "STOP" in r.stderr and "job succeeded" not in out and "OLD RESTORE RESULT" not in out, out
    assert not s["manifests"]


def test_step5_stops_when_old_pods_cannot_be_listed(kube):
    """查不到舊 Job 的 Pod（API 暫時失敗）不能當成「已經沒有」（#143 審查建議：new_job 沒看 kubectl get 的結束碼）。"""
    _restore_job_yaml(kube)
    kube.state(pods_error=True)
    r, s = kube.run(prep() + "\n" + step5())
    assert "STOP" in r.stderr and "job succeeded" not in r.stdout, r.stdout + r.stderr
    assert not s["manifests"]


def test_step5_stops_when_create_fails(kube):
    rjob = _restore_job_yaml(kube)
    kube.state(create_error='Error from server (Forbidden): error when creating "restore-job.yaml": jobs.batch '
                            f'"{rjob}" is forbidden: exceeded quota: audit-quota\n')
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "STOP" in r.stderr and "exceeded quota" in r.stderr, out
    assert "job succeeded" not in out and "job failed" not in out and "NO POD" not in out, out
    assert not _polls(s, rjob), "建立失敗就不輪詢"


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


@pytest.mark.parametrize("purpose", ["image", "source"])
def test_preflight_blocks_do_not_report_a_previous_check(kube, purpose):
    name = kube.names()[purpose]
    kube.state(**_old_job(name, '{"preflight": "image_ok", "from": "OLD CHECK"}'),
               next_run={"succeeded": 1, "failed": 0, "cond": "", "exit": 0,
                         "log": '{"preflight": "image_ok", "from": "NEW CHECK"}'})
    r, s = kube.run(prep() + "\n" + block("bash", f"name: $PJOB_{purpose.upper()}"),
                    IMAGE="registry.example/odoo-mcp-admin@sha256:" + "0" * 64)
    out = r.stdout + r.stderr
    assert "OLD CHECK" not in out and "NEW CHECK" in out, out
    assert name not in s["jobs"], "檢查完要刪掉 Job"
    assert f"name: {name}" in s["manifests"][0] and f"configMap: {{name: {kube.names()['configmap']}}}" in s["manifests"][0]


def test_job_blocks_never_apply_jobs_and_always_pass_the_job_uid():
    for kind, body in _blocks():
        if kind != "bash":
            continue
        assert "apply -f" not in body, body[:200]
        for line in body.splitlines():
            if re.match(r"\s*(wait_job|job_report) \S", line):
                assert '"$JOB_UID"' in line, line


def test_bash_blocks_parse():
    for kind, body in _blocks():
        if kind == "bash":
            r = subprocess.run(["bash", "-n"], input=body, text=True, capture_output=True)
            assert r.returncode == 0, (r.stderr, body[:300])


# ---------------------------------------------------------------------------------------------------------------
# 上線前重測 P3：同一個 namespace 的兩台服務各用各的 Job 與 ConfigMap，還原與步驟 13 不會互相刪掉


def test_job_and_configmap_names_carry_the_release(kube):
    assert kube.names("svc-1e199474") == {
        "restore": "mcp-restore-inplace-svc-1e199474", "image": "mcp-restore-preflight-image-svc-1e199474",
        "source": "mcp-restore-preflight-source-svc-1e199474", "configmap": "mcp-restore-preflight-svc-1e199474"}
    assert kube.names("hs324")["restore"] == "mcp-restore-inplace-hs324"


def test_long_or_unusual_release_names_stay_valid_and_distinct(kube):
    """Job 名稱會成為 Pod 的 job-name 標籤值：最多 63 字元、DNS-1123 label。過長或有其他字元的 REL 改用雜湊，不同 REL 不同名。"""
    long_a, long_b = "svc-" + "a" * 45 + "-one", "svc-" + "a" * 45 + "-two"   # 53 字元，截斷後前段相同
    rels = [long_a, long_b, "my.release", "my-release", "x" * 34, "x" * 35, "Svc-Upper", "a-" * 20 + "b"]
    seen = {}
    for rel in rels:
        got = kube.names(rel)
        for purpose, name in got.items():
            assert DNS_LABEL.fullmatch(name) and len(name) <= 63, (rel, name)
            stem = {"restore": "inplace", "image": "preflight-image", "source": "preflight-source",
                    "configmap": "preflight"}[purpose]
            assert name.startswith(f"mcp-restore-{stem}-"), name
            assert seen.setdefault((purpose, name), rel) == rel, f"{rel} 與 {seen[(purpose, name)]} 同名"
        assert kube.names(rel) == got, "同一個 REL 每次都得到同一個名稱"
    assert kube.names("x" * 34)["source"] == "mcp-restore-preflight-source-" + "x" * 34, "放得下就不雜湊"
    assert kube.names("x" * 35)["source"] != "mcp-restore-preflight-source-" + "x" * 35


def test_names_refuse_an_empty_release(kube):
    r, _ = kube.run('printf "\\n[%s]" "$RJOB"', REL="")
    assert "STOP" in r.stderr and r.stdout.splitlines()[-1] == "[]", r.stdout + r.stderr


def test_restoring_another_service_in_the_same_namespace_leaves_this_job_alone(kube):
    """重測實際發生：同一個 namespace 另一台服務的步驟 5 刪掉了這台已完成的還原 Job，這台的步驟 13 也刪掉了另一台的。"""
    other = kube.names("svc-other")
    rjob = _restore_job_yaml(kube)
    state = _job(other["restore"], OTHER_UID, "OTHER SERVICE RESTORE")
    kube.state(**state, configmaps=[other["configmap"]])
    r, s = kube.run(prep() + "\n" + step5())
    assert "job succeeded" in r.stdout and "NEW RUN" in r.stdout, r.stdout + r.stderr
    assert s["jobs"][other["restore"]]["uid"] == OTHER_UID and any(p["owner"] == OTHER_UID for p in s["pods"])
    assert other["configmap"] in s["configmaps"] and rjob in s["jobs"]
    assert not any(other["restore"] in " ".join(call) or other["configmap"] in " ".join(call) for call in s["calls"])

    r, s = kube.run(step13())
    assert r.returncode == 0, r.stdout + r.stderr
    assert rjob not in s["jobs"] and kube.names()["configmap"] not in s["configmaps"]
    assert s["deleted_pvcs"] == ["svc-runbook-test-restore-source"]
    assert s["jobs"][other["restore"]]["uid"] == OTHER_UID and other["configmap"] in s["configmaps"], "另一台的不動"


@pytest.mark.parametrize("purpose", ["image", "source"])
def test_preflight_of_another_service_is_left_alone(kube, purpose):
    other = kube.names("svc-other")
    kube.state(**_job(other[purpose], OTHER_UID, '{"preflight": "image_ok", "from": "OTHER"}'),
               configmaps=[other["configmap"]])
    r, s = kube.run(prep() + "\n" + block("bash", f"name: $PJOB_{purpose.upper()}"),
                    IMAGE="registry.example/odoo-mcp-admin@sha256:" + "0" * 64)
    assert "NEW RUN" in r.stdout and "OTHER" not in r.stdout, r.stdout + r.stderr
    assert s["jobs"][other[purpose]]["uid"] == OTHER_UID and other["configmap"] in s["configmaps"]


def test_step5_refuses_a_restore_job_yaml_with_another_name(kube):
    """restore-job.yaml 還是舊版的固定名稱（或填了別台的 REL）：什麼都不刪、不建，先停。"""
    _restore_job_yaml(kube, name="mcp-restore-inplace")
    kube.state(**_job("mcp-restore-inplace", OTHER_UID, "OTHER SERVICE RESTORE"))
    r, s = kube.run(prep() + "\n" + step5())
    assert "STOP" in r.stderr and "metadata.name" in r.stderr, r.stdout + r.stderr
    assert not s["manifests"] and s["jobs"]["mcp-restore-inplace"]["uid"] == OTHER_UID
    assert not _call_index(s, "delete", "job/mcp-restore-inplace")


def test_job_blocks_use_the_release_names_everywhere():
    """手冊的 bash 與 yaml 區塊不再直接寫固定的 Job 與 ConfigMap 名稱（註解與說明文字裡提到舊名稱沒關係）。"""
    fixed = re.compile(r"mcp-restore-(inplace|preflight)")
    for kind, body in _blocks():
        if kind in ("bash", "yaml"):
            code = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
            assert not fixed.search(code), fixed.search(code).group(0)


# ---------------------------------------------------------------------------------------------------------------
# wait_job：Job 被刪掉時立刻停（#143 二次審查：原本要等約 31 分鐘才印 job wait timeout）


def test_wait_job_stops_at_once_when_the_job_is_deleted(kube):
    kube.state(jobs={"mcp-restore-inplace-x": {"uid": OLD_UID, "succeeded": 0, "failed": 0, "cond": "", "vanish": True}})
    r, s = kube.run(prep() + f'\nwait_job mcp-restore-inplace-x {OLD_UID} 372; echo "rc=$?"')
    assert "rc=4" in r.stdout and "STOP" in r.stderr and "不見了" in r.stderr, r.stdout + r.stderr
    assert "job wait timeout" not in r.stdout and len(_polls(s, "mcp-restore-inplace-x")) == 1


def test_wait_job_keeps_polling_through_api_errors(kube):
    kube.state(jobs={"mcp-restore-inplace-x": {"uid": OLD_UID, "succeeded": 1, "failed": 0, "cond": "", "flaky": 3}})
    r, s = kube.run(prep() + f'\nwait_job mcp-restore-inplace-x {OLD_UID} 10; echo "rc=$?"')
    assert "rc=0" in r.stdout and "job succeeded" in r.stdout and "STOP" not in r.stderr, r.stdout + r.stderr
    assert len(_polls(s, "mcp-restore-inplace-x")) == 4


def test_step5_stops_at_once_when_the_restore_job_is_deleted_while_waiting(kube):
    rjob = _restore_job_yaml(kube)
    kube.state(next_run={"succeeded": 0, "failed": 0, "cond": "", "exit": None, "log": "", "vanish": True})
    r, s = kube.run(prep() + "\n" + step5())
    out = r.stdout + r.stderr
    assert "STOP" in r.stderr and "不見了" in r.stderr and "job wait timeout" not in out, out
    assert len(_polls(s, rjob)) == 1


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
                methods=None, backend=None, connection=None) -> dict:
        """methods：Reviewed Odoo methods（mcp_server.odoo_allowed_methods），None＝沒有存過；backend：Odoo 的後端帳號；
        connection：整個 connection 照給的值（其他產品）。"""
        env = dict(switches or {})
        proxy = {"timeout": 86400}
        if n8n:
            env["AUTH_TOKEN"] = proxy["bearer_token"] = self._secret()
        server = {"command": "woow-odoo-mcp", "args": [], "port": port, "env": env, "env_prefix": ""}
        if methods is not None:
            server["odoo_allowed_methods"] = list(methods)
        if connection is None:
            connection = {**(_backend(pid) if backend is None else backend), "odoo_password": self._secret()}
        return {"admin_password": self._secret(),
                "mcp_auth_token": self._secret() if token == "active" else "",
                "mcp_auth_revoked": token == "revoked",
                "mcp_auth_generation": self._secret(), "admin_session_generation": self._secret(),
                "token_history": [{"action": "auto_issue", "at": 1}] if token == "active" else [],
                "connection": connection,
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


def _inventory(volume: Volume, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    script = cwd / "profile-inventory.py"
    script.write_text(inventory_script())
    return subprocess.run([sys.executable, "-I", "-S", "-", "inventory", *args], input=script.read_text(), text=True,
                          capture_output=True, env={"MCP_ADMIN_CONFIG": volume.config, "LC_ALL": "C.UTF-8"})


def _diff(cwd: Path, before: str, restored: str, *rest: str, uid: str | None = None) -> subprocess.CompletedProcess:
    """diff <步驟 1 的檔> <還原後的檔> <還原 Job uid> [<現在的檔>]；uid 沒給就用 UID（_snapshot 的預設）。"""
    return subprocess.run([sys.executable, "-I", str(cwd / "profile-inventory.py"), "diff", before, restored,
                           UID if uid is None else uid, *rest], cwd=cwd, text=True, capture_output=True,
                          env={"LC_ALL": "C.UTF-8"})


UID, UID2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
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
    assert data["restore_job_uid"] is None, "步驟 1 的清單不屬於任何一次還原"


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


def _snapshot(volume: Volume, cwd: Path, name: str, uid: str | None = None) -> str:
    """把現在的設定卷記成 *name*；*uid*：步驟 9 第一段記下的還原 Job uid（還原後的紀錄才有）。"""
    r = _inventory(volume, cwd, *([uid] if uid else []))
    assert r.returncode == 0, r.stderr
    (cwd / name).write_text(r.stdout)
    return name


def test_diff_lists_every_kind_of_drift_after_the_restore(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write(*RESTORED)
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
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
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    volume.write(*RECONCILED)
    now = _snapshot(volume, tmp_path, "profiles-now.json")
    r = _diff(tmp_path, before, restored, now)
    assert r.returncode == 0, r.stdout
    assert r.stdout.strip().splitlines()[-1].startswith("RESULT: OK")
    assert re.search(rf"'mp-E'（id {E}）.*{E2}", r.stdout), "列出哪個帳號是核對時重建的"


def test_diff_keeps_flagging_what_is_still_different(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write(*RESTORED)
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    default, extra = RECONCILED
    # 重建的帳號政策沒設好、還原帶回來的 D 還在、多建了一個沒在步驟 1 的帳號
    wrong = [dict(p, disabled=["fit_gap_report"]) if p["id"] == E2 else p for p in extra]
    wrong += [RESTORED[1][2], {"id": _pid(), "label": "extra", "switches": WRITES_ON}]
    volume.write(default, wrong)
    now = _snapshot(volume, tmp_path, "profiles-now.json")
    r = _diff(tmp_path, before, restored, now)
    assert r.returncode == 1
    assert _tags(r.stdout) == sorted(["工具", "刪除", "多出"]), r.stdout


def test_diff_does_not_take_a_restored_profile_for_a_recreated_one(tmp_path):
    """備份後刪掉 D、又新增同名的 E：還原帶回來的 D 不能當成重建的 E（它帶著備份裡的後端連線）。"""
    volume = Volume(tmp_path / "data")
    volume.write({}, [{"id": E, "label": "mp-D"}])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"token": "revoked"}, [{"id": D, "label": "mp-D", "token": "revoked"}])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    r = _diff(tmp_path, before, restored)
    assert r.returncode == 1
    assert _tags(r.stdout) == ["刪除", "重建"], r.stdout


def test_diff_flags_restored_profiles_whose_token_is_not_revoked(tmp_path):
    """還原後、發 token 之前，還原回來的每個帳號都應該是已撤銷；不是就代表步驟 5 其實沒有執行（P3-d）。"""
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    r = _diff(tmp_path, before, restored)
    assert r.returncode == 1
    assert _tags(r.stdout) == ["token"] * 4, r.stdout
    assert r.stdout.count("回到步驟 4、5") == 4, r.stdout


def test_diff_refuses_missing_files(tmp_path):
    (tmp_path / "profile-inventory.py").write_text(inventory_script())
    r = _diff(tmp_path, "profiles-before.json", "profiles-restored.json")
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
    (kube.work / "restore-job.uid").write_text(UID + "\n")   # 步驟 5 建立還原 Job 時寫的
    check = block("bash", "> profiles-restored.json.tmp")
    r, _ = kube.run(shell + check)
    assert "RESULT: 6 項差異" in r.stdout, r.stdout + r.stderr
    assert json.loads((kube.work / "profiles-restored.json").read_text())["restore_job_uid"] == UID
    r, _ = kube.run(shell + step1)
    assert "STOP" in r.stderr and (kube.work / "profiles-before.json").read_text() == before_text

    volume.write(*RECONCILED)
    final = block("bash", "> profiles-now.json")
    r, _ = kube.run(shell + final)
    assert r.stdout.strip().splitlines()[-1].startswith("RESULT: OK"), r.stdout + r.stderr
    _assert_no_secret(volume, r.stdout + r.stderr + "".join(p.read_text() for p in kube.work.glob("*.json")))


# ---------------------------------------------------------------------------------------------------------------
# #143 二次審查：重跑步驟 5 之後忘了把 profiles-restored.json 改名，還原帶回來的帳號會被當成核對時重建的


def test_inventory_records_the_restore_job_uid(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*RESTORED)
    r = _inventory(volume, tmp_path, UID)
    assert r.returncode == 0 and json.loads(r.stdout)["restore_job_uid"] == UID


def _second_restore(tmp_path: Path) -> tuple[Volume, str, str]:
    """第一次還原後記了 profiles-restored.json；之後換一份備份重跑步驟 5，帶回了一個同名的舊帳號 X（id 不同）。"""
    volume = Volume(tmp_path / "data")
    volume.write({}, [{"id": E, "label": "mp-E"}])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"token": "revoked"}, [])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    volume.write({"token": "revoked"}, [{"id": D, "label": "mp-E", "token": "revoked"}])
    return volume, before, restored


def test_diff_refuses_a_restored_snapshot_from_another_restore(tmp_path):
    volume, before, restored = _second_restore(tmp_path)
    now = _snapshot(volume, tmp_path, "profiles-now.json")
    r = _diff(tmp_path, before, restored, now, uid=UID2)   # restore-job.uid 已是第二次還原的 Job
    assert r.returncode == 2 and "STOP" in r.stdout and UID in r.stdout and UID2 in r.stdout, r.stdout
    assert "RESULT" not in r.stdout and "[已重建]" not in r.stdout
    r = _diff(tmp_path, before, before, uid=UID2)   # 沒有記 uid 的檔（例如步驟 1 的）也不能當成還原後的紀錄
    assert r.returncode == 2 and "STOP" in r.stdout, r.stdout


def test_diff_refuses_a_revoked_profile_missing_from_the_restored_snapshot(tmp_path):
    """uid 對得上（例如 restore-job.uid 也是舊的），但現在有一個步驟 1 沒有、還原後紀錄也沒有、token 是 revoked 的帳號：
    核對時新建的帳號一定是 active，所以它是還原帶回來的。修正前它被當成重建的 'mp-E'，比對印 RESULT: OK。"""
    volume, before, restored = _second_restore(tmp_path)
    now = _snapshot(volume, tmp_path, "profiles-now.json")
    r = _diff(tmp_path, before, restored, now)
    assert r.returncode == 2 and "STOP" in r.stdout and D in r.stdout, r.stdout
    assert "RESULT" not in r.stdout and "[已重建]" not in r.stdout


def test_step5_and_step9_bind_the_snapshot_to_the_restore_job(kube):
    """手冊字面：步驟 5 寫 restore-job.uid，步驟 9 第一段把它記進 profiles-restored.json；重跑步驟 5 之後沒改名就停。"""
    volume = Volume(kube.tmp / "data")
    volume.write(*BEFORE)
    (kube.work / "profile-inventory.py").write_text(inventory_script())
    _restore_job_yaml(kube)
    kube.state(config=volume.config)
    shell = 'DEPLOY=svc-runbook-test-mcp-server\nCONTAINER=mcp-server\n'
    kube.run(shell + block("bash", "> profiles-before.json.tmp"))
    r, s = kube.run(prep() + "\n" + step5())
    first = (kube.work / "restore-job.uid").read_text().strip()
    assert first and f"created uid={first}" in r.stdout, r.stdout + r.stderr

    volume.write(*RESTORED)
    part1 = block("bash", "> profiles-restored.json.tmp")
    r, _ = kube.run(shell + part1)
    assert "RESULT: 6 項差異" in r.stdout, r.stdout + r.stderr

    r, s = kube.run(prep() + "\n" + step5())   # 例如發現選錯備份，再還原一次
    second = (kube.work / "restore-job.uid").read_text().strip()
    assert second != first
    r, _ = kube.run(shell + part1)
    assert "沿用已存的" in r.stdout and "STOP" in r.stdout and second in r.stdout, r.stdout + r.stderr
    assert "RESULT" not in r.stdout

    (kube.work / "profiles-restored.json").rename(kube.work / f"profiles-restored.{first}.json")
    r, _ = kube.run(shell + part1)
    assert "RESULT: 6 項差異" in r.stdout, r.stdout + r.stderr
    assert json.loads((kube.work / "profiles-restored.json").read_text())["restore_job_uid"] == second


def test_step9_stops_without_the_restore_job_uid(kube):
    volume = Volume(kube.tmp / "data")
    volume.write(*RESTORED)
    (kube.work / "profile-inventory.py").write_text(inventory_script())
    kube.state(config=volume.config)
    r, _ = kube.run('DEPLOY=d\nCONTAINER=c\n' + block("bash", "> profiles-restored.json.tmp"))
    assert "STOP" in r.stdout + r.stderr and "RESULT" not in r.stdout, r.stdout + r.stderr
    assert not (kube.work / "profiles-restored.json").exists()


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
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
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
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    r = _diff(tmp_path, before, restored)
    assert r.returncode == 1 and _tags(r.stdout) == ["方法"], r.stdout


def test_diff_flags_a_backend_account_switched_back_by_the_restore(tmp_path):
    """備份之後 B 改用受限的 Odoo 使用者：還原把連線（連同密碼）換回備份當時的使用者。發 token 之前就要看得出來，
    而且不印出任何密碼（審查 must-fix 2）。"""
    volume = Volume(tmp_path / "data")
    volume.write({}, [{"id": B, "label": "mp-B", "backend": {**_backend(B), "odoo_username": "mp-b-restricted"}}])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"token": "revoked"}, [{"id": B, "label": "mp-B", "token": "revoked"}])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
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


def test_inventory_masks_everything_up_to_the_last_at_sign(tmp_path):
    """#143 二次審查：密碼裡有沒編碼的 ? 或 # 時，舊的 url() 印出 https://admin:S3cr?***，留下帳號與密碼前段。
    值裡有 @ 就一律遮到最後一個 @；@ 前面已經有 ? 或 # 時分不出那是帳密還是 query，後面也一律遮（不能讓 query 的值露出來）。"""
    volume = Volume(tmp_path / "data")
    cases = {   # 網址 → 帳號清單記的值
        f"https://mp-admin:S3cr?t-{volume._secret()}@odoo.example.invalid": "https://***@***",
        f"https://mp-admin:S3cr#t-{volume._secret()}@odoo.example.invalid/odoo": "https://***@***",
        f"https://mp-admin:p@ss-{volume._secret()}@odoo.example.invalid:8069/odoo?db=x": "https://***@odoo.example.invalid:8069/odoo?***",
        f"mp-admin:{volume._secret()}@odoo.example.invalid:8069": "***@odoo.example.invalid:8069",
        f"https://odoo.example.invalid/web?login=mp-admin@example.invalid&key={volume._secret()}": "https://***@***",
        f"https://odoo.example.invalid/odoo#mp-admin@{volume._secret()}": "https://***@***",
        "https://odoo.example.invalid:8069/odoo": "https://odoo.example.invalid:8069/odoo",
    }
    pids = [_pid() for _ in cases]
    volume.write({}, [{"id": pid, "label": f"p{n}", "backend": {**_backend(pid), "odoo_url": value}}
                      for n, (pid, value) in enumerate(zip(pids, cases))])
    r = _inventory(volume, tmp_path)
    assert r.returncode == 0, r.stderr
    _assert_no_secret(volume, r.stdout + r.stderr)
    assert "mp-admin" not in r.stdout + r.stderr and "S3cr" not in r.stdout + r.stderr
    rows = {p["id"]: p for p in json.loads(r.stdout)["profiles"]}
    assert [rows[pid]["backend"]["odoo_url"] for pid in pids] == list(cases.values())


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
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    r = _diff(tmp_path, before, restored)
    line = next(x for x in r.stdout.splitlines() if x.startswith("[重建]"))
    recorded = next(p for p in json.loads((tmp_path / before).read_text())["profiles"] if p["id"] == E)
    for field in [*POLICY_FIELDS.values(), "backend"]:
        assert repr(recorded[field]) in line, (field, line)

    # 剛重建：新的 id，政策複製預設帳號（寬的方法清單、沒有停用工具與操作、預設帳號的開關），連線是空的
    copied = {"id": E2, "label": "mp-E", "switches": WRITES_ON, "methods": wide,
              "backend": dict.fromkeys(_backend(E), "")}
    volume.write({"switches": WRITES_ON, "methods": wide, "token": "revoked"}, [copied])
    r = _diff(tmp_path, before, restored, _snapshot(volume, tmp_path, "profiles-now.json"))
    assert r.returncode == 1
    assert _tags(r.stdout) == sorted(["工具", "操作", "開關", "方法", "後端", "後端", "後端"]), r.stdout

    # 照 [重建] 列設定好政策、在 Connection 頁填步驟 1 的後端帳號
    volume.write({"switches": WRITES_ON, "methods": wide, "token": "revoked"},
                 [dict(step1, id=E2, backend=_backend(E))])
    r = _diff(tmp_path, before, restored, _snapshot(volume, tmp_path, "profiles-now.json"))
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1].startswith("RESULT: OK"), r.stdout
    _assert_no_secret(volume, r.stdout + r.stderr)


# ---------------------------------------------------------------------------------------------------------------
# 上線前重測 P3：Odoo 以外的產品，步驟 9 也要比對得到後端（重建的 LiteLLM 帳號還沒填連線時只印 [工具]、甚至 RESULT: OK）


def _connections(volume: Volume) -> dict:
    """各產品的合成 connection（秘密欄位都是 Volume 記下的隨機值）與帳號清單應記的後端欄位。"""
    s = volume._secret
    return {
        "odoo": ({"odoo_url": "https://odoo-a.example.invalid", "odoo_db": "db_a", "odoo_username": "mp-odoo-a",
                  "odoo_password": s()},
                 {"odoo_url": "https://odoo-a.example.invalid", "odoo_db": "db_a", "odoo_username": "mp-odoo-a"}),
        "litellm": ({"base_url": "https://litellm-a.example.invalid", "master_key": s()},
                    {"base_url": "https://litellm-a.example.invalid"}),
        "nextcloud": ({"base_url": "https://cloud.example.invalid", "username": "mp-nc-a", "app_password": s()},
                      {"base_url": "https://cloud.example.invalid", "username": "mp-nc-a"}),
        "hermes": ({"gateway_url": "https://gw.example.invalid", "gateway_api_key": s(),
                    "dashboard_url": "https://dash.example.invalid", "dashboard_username": "mp-hermes-a",
                    "dashboard_password": s()},
                   {"gateway_url": "https://gw.example.invalid", "dashboard_url": "https://dash.example.invalid",
                    "dashboard_username": "mp-hermes-a"}),
        "n8n": ({"n8n_api_url": "https://n8n.example.invalid/api/v1", "n8n_api_key": s(), "n8n_mcp_access_token": s(),
                 "mcp_session_timeout": "3600", "mcp_max_sessions": "10"},
                {"n8n_api_url": "https://n8n.example.invalid/api/v1"}),
        "emqx": ({"base_url": "https://emqx.example.invalid:18083", "api_key": s(), "api_secret": s()},
                 {"base_url": "https://emqx.example.invalid:18083"}),
        "headscale": ({"base_url": "https://hs.example.invalid", "api_key": s(), "enrollment_url": "https://login.example.invalid",
                       "headplane_url": "", "paas_base_url": "https://paas.example.invalid", "workspace_id": "12",
                       "headscale_service_id": "34", "paas_api_token": s()},
                      {"base_url": "https://hs.example.invalid", "paas_base_url": "https://paas.example.invalid",
                       "workspace_id": "12", "headscale_service_id": "34"}),
        "opendesign": ({"od_api_base": f"https://od-user:{s()}@od.example.invalid", "od_basic_user": "od-user",
                        "od_basic_password": s(), "od_chat_timeout": "120"},
                       {"od_api_base": "https://***@od.example.invalid", "od_basic_user": "od-user"}),
        "vk": ({"vibe_backend_url": "https://vk.example.invalid"}, {"vibe_backend_url": "https://vk.example.invalid"}),
        "k8s": ({"kubeconfig": "apiVersion: v1\nusers:\n- user:\n    token: " + s(), "in_cluster": "false"},
                {"in_cluster": "false"}),
    }


def test_inventory_records_the_backend_of_every_product(tmp_path):
    volume = Volume(tmp_path / "data")
    products = _connections(volume)
    pids = {app: _pid() for app in products}
    volume.write({}, [{"id": pids[app], "label": app, "connection": conn} for app, (conn, _) in products.items()])
    r = _inventory(volume, tmp_path)
    assert r.returncode == 0, r.stderr
    _assert_no_secret(volume, r.stdout + r.stderr)
    rows = {p["id"]: p for p in json.loads(r.stdout)["profiles"]}
    assert {app: rows[pids[app]]["backend"] for app in products} == {app: want for app, (_, want) in products.items()}
    assert set(products) == set(_apps()), "每個產品都要有一組合成 connection"


def test_diff_flags_a_recreated_litellm_profile_without_connection(tmp_path):
    """重測 S3：LiteLLM 備份後新增 C（接後端 c），還原後以同名重建、只設工具沒填連線。修正前第二段只看工具，印 RESULT: OK。"""
    volume = Volume(tmp_path / "data")
    conn = lambda name: {"base_url": f"https://litellm-{name}.example.invalid", "master_key": volume._secret()}
    volume.write({"connection": conn("a")}, [{"id": C, "label": "mp-C", "disabled": ["litellm_list_users"],
                                              "connection": conn("c")}])
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"connection": conn("a"), "token": "revoked"}, [])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    r = _diff(tmp_path, before, restored)
    assert re.search(r"^\[重建\] 'mp-C'.*'base_url': 'https://litellm-c.example.invalid'", r.stdout, re.M), r.stdout
    empty = {"base_url": "", "master_key": ""}   # profiles.py 的 create：每個連線欄位都是空字串
    volume.write({"connection": conn("a"), "token": "revoked"},
                 [{"id": E2, "label": "mp-C", "disabled": ["litellm_list_users"], "connection": empty}])
    r = _diff(tmp_path, before, restored, _snapshot(volume, tmp_path, "profiles-now.json"))
    assert r.returncode == 1 and _tags(r.stdout) == ["後端"], r.stdout
    assert re.search(r"^\[後端\] 'mp-C'.*base_url 步驟 1 是 'https://litellm-c.example.invalid'，現在是 None", r.stdout, re.M)
    _assert_no_secret(volume, r.stdout + r.stderr)


def test_empty_backend_fields_are_not_recorded(tmp_path):
    """sandbox（fix3-restore-20261113）：chart 預設 seed 會在 LiteLLM 的設定留下 Hermes 的空 gateway_url／dashboard_url。
    空的欄位不記：摘要乾淨，舊版腳本記下的空欄位與新的紀錄比對也不會算差異。"""
    volume = Volume(tmp_path / "data")
    seeded = {"gateway_url": "", "dashboard_url": "", "base_url": "https://litellm-a.example.invalid",
              "master_key": volume._secret()}
    volume.write({"connection": seeded}, [{"id": C, "label": "mp-C", "connection": {"base_url": "", "master_key": ""}}])
    r = _inventory(volume, tmp_path)
    rows = {p["id"]: p for p in json.loads(r.stdout)["profiles"]}
    assert rows["default"]["backend"] == {"base_url": "https://litellm-a.example.invalid"}
    assert rows[C]["backend"] == {} and "此產品需人工確認後端" in r.stderr
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    data = json.loads((tmp_path / before).read_text())   # 舊版腳本的紀錄：空欄位也記了
    data["profiles"][0]["backend"].update(gateway_url="", dashboard_url="")
    (tmp_path / before).write_text(json.dumps(data))
    volume.write({"connection": seeded, "token": "revoked"}, [{"id": C, "label": "mp-C", "token": "revoked",
                                                               "connection": {"base_url": "", "master_key": ""}}])
    r = _diff(tmp_path, before, _snapshot(volume, tmp_path, "profiles-restored.json", UID))
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1].startswith("RESULT: OK"), r.stdout
    _assert_no_secret(volume, r.stdout + r.stderr)


def test_recreate_line_says_manual_check_when_nothing_is_comparable(tmp_path):
    """沒有記到可比對的後端欄位（例如只有 kubeconfig 這種秘密）：印「此產品需人工確認後端」，不印看起來像不需要後端的 {}。"""
    volume = Volume(tmp_path / "data")
    secret_only = {"kubeconfig": "apiVersion: v1\nusers:\n- user:\n    token: " + volume._secret()}
    volume.write({"connection": secret_only}, [{"id": C, "label": "mp-C", "connection": dict(secret_only)}])
    r = _inventory(volume, tmp_path)
    assert r.returncode == 0 and r.stderr.count("此產品需人工確認後端") == 2, r.stderr
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write({"connection": secret_only, "token": "revoked"}, [])
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    r = _diff(tmp_path, before, restored)
    line = next(x for x in r.stdout.splitlines() if x.startswith("[重建]"))
    assert "此產品需人工確認後端" in line and "後端 {}" not in line and "後端帳號 {}" not in line, line
    _assert_no_secret(volume, r.stdout + r.stderr)


# ---------------------------------------------------------------------------------------------------------------
# #143 二次審查：步驟 12 重跑第二段時，[token] 行仍寫「停止，回到步驟 4、5」


def test_token_lines_after_step10_say_it_is_expected(tmp_path):
    volume = Volume(tmp_path / "data")
    volume.write(*BEFORE)
    before = _snapshot(volume, tmp_path, "profiles-before.json")
    volume.write(*RESTORED)
    restored = _snapshot(volume, tmp_path, "profiles-restored.json", UID)
    default, extra = RECONCILED
    issued = ({**default, "token": "active"}, [{**p, "token": "active"} for p in extra])   # 步驟 10 發了 token
    volume.write(*issued)
    r = _diff(tmp_path, before, restored, _snapshot(volume, tmp_path, "profiles-now.json"))
    assert r.returncode == 1 and _tags(r.stdout) == ["token"] * 3, r.stdout
    token_lines = [x for x in r.stdout.splitlines() if x.startswith("[token]")]
    assert all("步驟 10" in x and "預期" in x and "回到步驟 4、5" not in x for x in token_lines), token_lines
    assert r.stdout.strip().splitlines()[-1].startswith("RESULT: 只有 3 個 [token]"), r.stdout


# ---------------------------------------------------------------------------------------------------------------
# 帳號清單要記的欄位與各產品政策一致；core 與各 app 寫進 mcp_server、tools 的鍵都要歸類


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


# 後端帳號（IDENTITY）：各產品 connection 裡不是秘密、決定連到哪個後端或用哪個帳號的欄位。LaunchPolicy 宣告的身分、
# 必要與後端連線欄位（identity_connection_keys、required_connection、backend_connection）裡不是秘密的都要記，另外：
EXTRA_IDENTITY = {
    "od_basic_user": "Open Design 的 Basic Auth 帳號（選填，所以不在 backend_connection）",
    "paas_base_url": "Headscale 綁定的 PaaS 服務（網址＋workspace＋服務，搭配秘密的 paas_api_token）",
    "workspace_id": "同上",
    "headscale_service_id": "同上",
    "in_cluster": "k8s 用叢集內 ServiceAccount 還是 kubeconfig（kubeconfig 本身是秘密，比對不到）",
}
# 其他不是秘密的連線欄位與不記的原因（新增連線欄位卻沒有歸類時測試失敗）：
NOT_IDENTITY = {
    "enrollment_url": "Headscale Connection 頁顯示的裝置註冊連結，子程序不用、不帶憑證",
    "headplane_url": "Headscale Connection 頁顯示的 Headplane 連結，同上",
    "od_chat_timeout": "Open Design 的逾時設定",
    "mcp_session_timeout": "n8n 的 session 逾時，不是帳號（手冊剩餘風險段已列）",
    "mcp_max_sessions": "n8n 的 session 上限，同上",
}
# IDENTITY 裡不是網址、照原樣記的欄位（其餘都經 url() 遮掉帳密、query 與 fragment）：
NOT_URL = {"odoo_db", "odoo_username", "dashboard_username", "username", "workspace_id", "headscale_service_id",
           "od_basic_user", "in_cluster"}


def test_backend_account_fields_match_every_product_policy():
    """IDENTITY＝各 app LaunchPolicy 宣告的身分、必要與後端連線欄位裡不是秘密的鍵，加上 EXTRA_IDENTITY；其他不是秘密的
    連線欄位都要在 NOT_IDENTITY 寫明原因。密碼、API key、token、kubeconfig 這類欄位一律不讀出。"""
    from mcp_admin_core.routers.settings import _is_secret_key

    namespace = _script_namespace()
    identity, urls = namespace["IDENTITY"], namespace["URLS"]
    declared, public = set(), set()
    for policy in _launch_policies().values():
        keys = (set(policy.identity_connection_keys) | {key for key, _label in policy.required_connection}
                | {key for key, _label in policy.backend_connection})
        declared |= {key for key in keys if not _is_secret_key(key)}
        public |= {key for key in policy.connection_keys if not _is_secret_key(key)}
    assert identity and len(set(identity)) == len(identity)
    assert set(identity) == declared | set(EXTRA_IDENTITY), sorted(set(identity) ^ (declared | set(EXTRA_IDENTITY)))
    assert not any(_is_secret_key(key) for key in identity)
    assert not public - set(identity) - set(NOT_IDENTITY), (
        "新的連線欄位：是後端帳號就加進手冊 profile-inventory.py 的 IDENTITY，不是就寫進 NOT_IDENTITY 並寫明原因："
        f"{sorted(public - set(identity) - set(NOT_IDENTITY))}")
    assert set(EXTRA_IDENTITY) | set(NOT_IDENTITY) <= public, "表裡不要留沒有產品用的欄位"
    assert set(urls) == set(identity) - NOT_URL and len(set(urls)) == len(urls)


SECTIONS = ("mcp_server", "tools")
# core 與各 app 寫進 mcp_server、tools 的每一個鍵都要在下面歸類。restore guard 只重設憑證，這兩個區段的其他欄位都會被
# 還原帶回備份當時。
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
    ("tools", "owner_defaults_20261025"): "「預設全開」一次性遷移的標記（owner_defaults）；它改的開關與停用清單記在上表",
    ("tools", "restore_guard_preserve_policy_20261112"): "還原 guard 寫的保留政策標記，owner_defaults 啟動時移除",
}
# 看不出寫哪些鍵的 ConfigStore 寫入，每個呼叫一列：(檔案, 函式, 方法, 區段) → 它寫進 mcp_server、tools 的 (區段, 鍵)
# （也要在上面兩張表裡）。區段 "?"＝區段不是常數；"*"＝update／transform／save 寫整份設定。同一個函式再多一個同樣的
# 寫入也會讓測試失敗（2026-11-13 審查：原本以（檔案, 區段）登記，同區段多一個看不出鍵的寫入不會被發現）。
CORE, APPS = "packages/mcp-admin-core/mcp_admin_core/", "apps/"
OPAQUE_WRITES = {
    # PUT /api/settings/tools 的整包 body（管理頁不用這條，Tools、Permissions 頁走 /api/tools）：read_policy 只認這兩個政策鍵
    (CORE + "routers/settings.py", "put_section.apply_update", "patch", "tools"):
        {("tools", "disabled"), ("tools", "disabled_operations")},
    # 同一個函式的 else 分支：只會是 WRITABLE_SECTIONS 裡上面沒處理的區段（目前沒有，見下一個測試），不會是 mcp_server、tools
    (CORE + "routers/settings.py", "put_section.apply_update", "put", "?"): set(),
    # 新帳號：複製預設帳號當下的 mcp_server 與 tools（既有的鍵），只換 port 與 n8n 的 env.AUTH_TOKEN
    (CORE + "profiles.py", "ProfileRegistry.create", "update", "*"): {("mcp_server", "port"), ("mcp_server", "env")},
    # 帳號索引（profile_index）的新增、改名、刪除
    (CORE + "profiles.py", "ProfileRegistry.create", "transform", "*"): set(),
    (CORE + "profiles.py", "ProfileRegistry.rename", "transform", "*"): set(),
    (CORE + "profiles.py", "ProfileRegistry.delete", "transform", "*"): set(),
    # 「預設全開」一次性遷移：env 開關、兩個停用清單、標記；還原保留政策時只把 hold 標記換成正式標記
    (CORE + "owner_defaults.py", "apply_owner_defaults", "transform", "*"):
        {("mcp_server", "env"), ("tools", "disabled"), ("tools", "disabled_operations"),
         ("tools", "owner_defaults_20261025"), ("tools", "restore_guard_preserve_policy_20261112")},
    # 啟動時補 LITELLM_MCP_READONLY 與 env_prefix
    (APPS + "litellm/litellm_mcp_admin/seed_defaults.py", "apply_seed_defaults", "patch", "mcp_server"):
        {("mcp_server", "env"), ("mcp_server", "env_prefix")},
}
# await 的 patch／put／update／transform／save，接收者名稱沒有 store 的呼叫：寫明它不是 ConfigStore
NOT_CONFIG_STORE = {
    (APPS + "hermes/hermes_mcp_admin/routers/dashboard_proxy.py", "_dashboard_put", "client.put"):
        "httpx：轉發到 Hermes Dashboard API，不是設定檔",
}
STORE_METHODS = ("patch", "put", "update", "transform", "save")
STORE_IMPL = CORE + "config/store.py"   # ConfigStore 自己的實作（put 呼叫 self.update），不掃
STOREISH = re.compile(r"(?:^|[^a-z])store")   # store、self.default_store、get_config_store()、registry.store_for(…)；不含 restore


def _qualified(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """每個節點與它所在的函式（Class.method、外層.內層；模組層級是 <module>）。"""
    out: list[tuple[ast.AST, str]] = []

    def walk(node: ast.AST, qual: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            out.append((child, ".".join(qual) or "<module>"))
            walk(child, qual + [child.name] if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 else qual)

    walk(tree, [])
    return out


def _scan(sources: dict[str, str]) -> dict:
    """掃描 ``{相對路徑: 原始碼}`` 裡寫進 ConfigStore 的呼叫（patch／put／update／transform／save）。

    接收者名稱有 store 就當成 ConfigStore。區段與鍵可以是字串常值或模組層級的字串常數，參數可以是位置或關鍵字。回傳：
    - writes：{(區段, 鍵): {檔案}}，看得出鍵的寫入；
    - opaque：[(檔案, 函式, 方法, 區段)]，看不出鍵的寫入，每個呼叫一筆（區段 "?"＝區段不是常數，"*"＝整份設定）；
    - foreign：[(檔案, 函式, "接收者.方法")]，await 的同名方法、接收者名稱沒有 store（例如 httpx 的 client.put）；
    - loose：[(檔案, 函式, "接收者.方法")]，沒有直接呼叫、被當成值傳出去的 store 寫入方法。
    """
    writes: dict = {}
    opaque, foreign, loose = [], [], []
    for rel, text in sorted(sources.items()):
        tree = ast.parse(text)
        consts = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                  and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                  for target in node.targets if isinstance(target, ast.Name)}
        nodes = _qualified(tree)
        awaited = {id(node.value) for node, _ in nodes if isinstance(node, ast.Await)}
        called = {id(node.func) for node, _ in nodes if isinstance(node, ast.Call)}

        def const(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            return consts.get(node.id) if isinstance(node, ast.Name) else None

        def record(section, value, where):
            if isinstance(value, ast.Dict) and all(k is not None and const(k) is not None for k in value.keys):
                for key in value.keys:
                    writes.setdefault((section, const(key)), set()).add(rel)
            else:
                opaque.append(where + (section,))

        for node, qual in nodes:
            if (isinstance(node, ast.Attribute) and node.attr in STORE_METHODS and id(node) not in called
                    and STOREISH.search(ast.unparse(node.value).lower())):
                loose.append((rel, qual, ast.unparse(node)))
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in STORE_METHODS):
                continue
            receiver, method = ast.unparse(node.func.value), node.func.attr
            if not STOREISH.search(receiver.lower()):
                if id(node) in awaited:
                    foreign.append((rel, qual, receiver + "." + method))
                continue
            where = (rel, qual, method)
            kw = {k.arg: k.value for k in node.keywords}
            if None in kw or any(isinstance(arg, ast.Starred) for arg in node.args):
                opaque.append(where + ("?",))
            elif method in ("patch", "put"):
                section_node = node.args[0] if node.args else kw.get("key")
                value = node.args[1] if len(node.args) > 1 else kw.get("updates" if method == "patch" else "value")
                section = const(section_node) if section_node is not None else None
                if section is None:
                    opaque.append(where + ("?",))
                elif section in SECTIONS:
                    record(section, value, where)
            elif method == "update":
                value = node.args[0] if node.args else kw.get("updates")
                if isinstance(value, ast.Dict) and None not in value.keys:
                    for key, section_value in zip(value.keys, value.values):
                        section = const(key)
                        if section is None:
                            opaque.append(where + ("?",))
                        elif section in SECTIONS:
                            record(section, section_value, where)
                else:
                    opaque.append(where + ("*",))
            else:   # transform、save：整份設定
                opaque.append(where + ("*",))
    return {"writes": writes, "opaque": opaque, "foreign": foreign, "loose": loose}


def _section_writes() -> dict:
    files = sorted((ROOT / "packages" / "mcp-admin-core" / "mcp_admin_core").rglob("*.py"))
    for app in _apps():
        files += sorted((ROOT / "apps" / app / (app.replace("-", "_") + "_mcp_admin")).rglob("*.py"))
    return _scan({path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in files
                  if path.relative_to(ROOT).as_posix() != STORE_IMPL})


@pytest.mark.parametrize(("code", "writes", "opaque"), [
    # 2026-11-13 審查：第一個參數不是字串常值、用關鍵字參數、經 update／transform／save 寫入的，原本都被略過
    ("SECTION = 'tools'\nasync def f(store):\n    await store.patch(SECTION, {'x': 1})\n", {("tools", "x")}, []),
    ("async def f(store):\n    await store.patch(key='tools', updates={'x': 1})\n", {("tools", "x")}, []),
    ("async def f(store, data):\n    await store.put('mcp_server', value=data)\n", set(), [("f", "put", "mcp_server")]),
    ("async def f(store, section):\n    await store.patch(section, {'x': 1})\n", set(), [("f", "patch", "?")]),
    ("async def f(store, kw):\n    await store.patch('tools', **kw)\n", set(), [("f", "patch", "?")]),
    ("async def f(store):\n    await store.update({'tools': {'x': 1}, 'admin_password': 'p'})\n", {("tools", "x")}, []),
    ("async def f(store, v):\n    await store.update({'mcp_server': v})\n", set(), [("f", "update", "mcp_server")]),
    ("async def f(store, cfg):\n    await store.update(cfg)\n", set(), [("f", "update", "*")]),
    ("class R:\n    async def g(self):\n        await self.default_store.transform(lambda c: c)\n", set(),
     [("R.g", "transform", "*")]),
    ("async def f(store, cfg):\n    await get_config_store().save(cfg)\n", set(), [("f", "save", "*")]),
    # 同一個函式、同一個區段多一個看不出鍵的寫入：逐一列出（原本以（檔案, 區段）登記，看不出多了一個）
    ("async def f(store, a, b):\n    await store.patch('tools', a)\n    await store.patch('tools', b)\n", set(),
     [("f", "patch", "tools"), ("f", "patch", "tools")]),
    ("async def f(store):\n    await store.patch('connection', {'x': 1})\n", set(), []),   # 其他區段不管
    ("def f(restored):\n    restored.update({'tools': 1})\n", set(), []),   # restore 不是 store、也不是 await
])
def test_scan_catches_every_way_of_writing_a_section(code, writes, opaque):
    found = _scan({"x.py": code})
    assert set(found["writes"]) == writes
    assert sorted(found["opaque"]) == sorted(("x.py",) + item for item in opaque)
    assert not found["foreign"] and not found["loose"]


def test_scan_reports_unknown_receivers_and_store_methods_passed_around():
    found = _scan({"x.py": "async def f(cs, router):\n    await cs.patch('tools', {'x': 1})\n"
                           "    @router.put('/x')\n    async def h():\n        pass\n"
                           "def g(store):\n    return store.patch\n"})
    assert found["foreign"] == [("x.py", "f", "cs.patch")], "名稱沒有 store 的 await 寫入要登記（router.put 不是 await）"
    assert found["loose"] == [("x.py", "g", "store.patch")]


def test_generic_settings_put_only_writes_known_sections():
    """OPAQUE_WRITES 裡 settings.py 的 put(section, body) 登記成不寫 mcp_server、tools，前提是每個可寫的區段都在它前面
    處理掉；新增可寫區段時要重新歸類。"""
    from mcp_admin_core.routers import settings

    assert settings.WRITABLE_SECTIONS == {"connection", "mcp_server", "proxy", "tools", "admin_password"}


def test_every_policy_field_written_by_the_apps_is_recorded():
    """2026-11-13 審查：Reviewed Odoo methods 存在 mcp_server.odoo_allowed_methods，舊版帳號清單沒記，還原放寬了清單也印
    RESULT: OK。之後新增每帳號政策欄位卻沒有同步 profile-inventory.py，或新增沒登記的寫入時，這個測試會失敗。"""
    scan = _section_writes()
    writes = scan["writes"]
    assert ("mcp_server", "odoo_allowed_methods") in writes, "掃描要找得到 Odoo 的 store.patch('mcp_server', {FIELD: …})"
    unknown = {key: sorted(files) for key, files in writes.items() if key not in POLICY_FIELDS and key not in NOT_POLICY}
    assert not unknown, ("新的 tools／mcp_server 欄位會被還原帶回備份當時：是每帳號政策就加進手冊 profile-inventory.py 的 "
                         f"policy() 與 FIELDS、步驟 9 的表，再加進 POLICY_FIELDS；不是就寫進 NOT_POLICY 並寫明原因：{unknown}")
    assert sorted(scan["opaque"]) == sorted(OPAQUE_WRITES), (
        "看不出鍵的寫入（區段不是常數、內容不是 dict 常值、update／transform／save）要逐一列進 OPAQUE_WRITES，寫明會寫"
        f"哪些鍵：多出 {sorted(set(scan['opaque']) - set(OPAQUE_WRITES))}、"
        f"已不存在 {sorted(set(OPAQUE_WRITES) - set(scan['opaque']))}、全部 {sorted(scan['opaque'])}")
    assert sorted(scan["foreign"]) == sorted(NOT_CONFIG_STORE), (
        f"接收者名稱沒有 store 的 await 寫入：是 ConfigStore 就改名或登記進 OPAQUE_WRITES，不是就登記進 NOT_CONFIG_STORE："
        f"{sorted(scan['foreign'])}")
    assert not scan["loose"], f"store 的寫入方法被當成值傳出去，掃描看不出它寫什麼：{scan['loose']}"
    declared = set(writes) | {key for keys in OPAQUE_WRITES.values() for key in keys}
    assert declared == set(POLICY_FIELDS) | set(NOT_POLICY), (
        f"表裡不要留沒有程式寫入的項目：{sorted((set(POLICY_FIELDS) | set(NOT_POLICY)) ^ declared)}")
    namespace = _script_namespace()
    recorded, compared = set(namespace["policy"]({})), {field for field, _tag, _where in namespace["FIELDS"]}
    for field in POLICY_FIELDS.values():
        assert field in recorded and field in compared, f"profile-inventory.py 沒有記錄或比對 {field}"
