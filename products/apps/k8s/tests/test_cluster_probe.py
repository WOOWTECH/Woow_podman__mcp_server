"""cluster_probe helper 的契約測試（Test 路徑二 / health probe 共用的 reachability）。

驗證重點：
- kubeconfig 的 server URL 與 CA 一律跟 current-context 走（不能抓第一個 cluster）。
- insecure-skip-tls-verify → verify=False（避免對自簽 CA 全面 false RED）。
- 單 context 無 current-context 時自動選（對齊上游 manager.go 行為）。
- in-cluster 分支：env KUBERNETES_SERVICE_HOST/PORT 組 URL、SA ca.crt 當 CA；env 缺
  時 fallback https://kubernetes.default.svc。
- fetch_version 三態：200+gitVersion → 可達有版本；401/403 → 可達無版本（匿名被拒）；
  連線錯誤 → 不可達（與認證失敗分層表述）。
"""
from __future__ import annotations

import ssl

import httpx
import pytest
import yaml

from k8s_mcp_admin import cluster_probe
from k8s_mcp_admin.cluster_probe import ProbeTarget, fetch_version, resolve_target

# 測試用有效自簽 CA（base64(PEM)）——resolve_target 會 base64 解碼後餵給 ssl context，
# 無效 PEM 會讓 ssl.create_default_context raise，故此處必須是真憑證。
_CA_B64 = (
    "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUREVENDQWZXZ0F3SUJBZ0lVVGhmT2dIMmdh"
    "Q2NiMEpNSG1hMDFWaWNjYWR3d0RRWUpLb1pJaHZjTkFRRUwKQlFBd0ZqRVVNQklHQTFVRUF3d0xh"
    "emh6TFhSbGMzUXRZMkV3SGhjTk1qWXdOekUyTVRReU1UUXpXaGNOTXpZdwpOekV6TVRReU1UUXpX"
    "akFXTVJRd0VnWURWUVFEREF0ck9ITXRkR1Z6ZEMxallUQ0NBU0l3RFFZSktvWklodmNOCkFRRUJC"
    "UUFEZ2dFUEFEQ0NBUW9DZ2dFQkFJOHpyQ3paNDdBYkdtZFFWck03V2g5SzBoVkJEeE9HZS85MUYv"
    "K04KZXpKdmNNZ1pmN0tXR3gyOUg5WW9ZUWVYOSttMXQ0QWxuTFZNOUIyc3drVFRDajNXa1l5TFNZ"
    "QUN4ZFVDUlFRVgoxUEJTZjFRTFUxS3RudlViQStJVkVDc1ZTL0VLc2djZnRLVjRSdERBMUt1Y1NO"
    "MjJ1YURBbzg5Tlpub0RVWWhBCmJkbHdsY29nbzYvaHd0OXczcDkvalUxczc2SmVUZ2paUzVkaGIy"
    "ejZVaDNYS0lQejdJTHdlMXltUmVGb25wT0EKcS9NL1A2OXlwRzQxKzRYemlncHpuV2JBYVpwQThP"
    "ZEJmQ3F2dEM5ZitLejV5Mmsyb1E3Nm9SdDd4bVdDd25zcgp0SWJocTRXRnNXTWhXakdSRWxndjhO"
    "ZWg2R1BXZlI3Z2JraGpTNnR2Nm91cmlJY0NBd0VBQWFOVE1GRXdIUVlEClZSME9CQllFRkJtL21W"
    "VWVYNENDQ0VmRThEY2tiU00vc2l0Y01COEdBMVVkSXdRWU1CYUFGQm0vbVZVZVg0Q0MKQ0VmRThE"
    "Y2tiU00vc2l0Y01BOEdBMVVkRXdFQi93UUZNQU1CQWY4d0RRWUpLb1pJaHZjTkFRRUxCUUFEZ2dF"
    "QgpBQXlxNmJ6bFdMY1VRZklZQTIvdW9VWlVYZFRGTk9aNlF3SFp5RWZPalVIejBxVXBnSVprbEYy"
    "a1FoZkNzbmpNCnEwek9IQlpXVkJpWU1WQXJuWkkvY1RUMkw5eHVMY2ZnUWk0SElEM3pob1hhbm5M"
    "dXJQVGlzZE9KSmU3QWhZZ0QKeFRYUjVGQll2cHNwdDk1REpiMHlzUjhjMVdrdDcwZk9LSjRNUEJ5"
    "T29TOFVsWGVzNU5KOExFbUhGQkVIVm9lUgprRUJPbm80QnA4RjBXeEQwVllDQmxNcUxBQkdPNzFB"
    "aUY5WTQrM2RFS0JtV2ppZ3NzS1FIUkZGVEVqcFRMUkxiCmdQMDJKUGZQbk9MVTl1cGFod1ZybWtt"
    "OXMzZmV2N1ZENk1qeHhTVWt2QmYvZGNSQU1zSllLRmdqQThNZlUxa0EKdjJqaEFBZXR3ei9iTHpy"
    "eWVFS3lNZk09Ci0tLS0tRU5EIENFUlRJRklDQVRFLS0tLS0K"
)

# in-cluster CA 檔用的 PEM 明文（同一張自簽憑證解碼）。
_CA_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDDTCCAfWgAwIBAgIUThfOgH2gaCcb0JMHma01ViccadwwDQYJKoZIhvcNAQEL\n"
    "BQAwFjEUMBIGA1UEAwwLazhzLXRlc3QtY2EwHhcNMjYwNzE2MTQyMTQzWhcNMzYw\n"
    "NzEzMTQyMTQzWjAWMRQwEgYDVQQDDAtrOHMtdGVzdC1jYTCCASIwDQYJKoZIhvcN\n"
    "AQEBBQADggEPADCCAQoCggEBAI8zrCzZ47AbGmdQVrM7Wh9K0hVBDxOGe/91F/+N\n"
    "ezJvcMgZf7KWGx29H9YoYQeX9+m1t4AlnLVM9B2swkTTCj3WkYyLSYACxdUCRQQV\n"
    "1PBSf1QLU1KtnvUbA+IVECsVS/EKsgcftKV4RtDA1KucSN22uaDAo89NZnoDUYhA\n"
    "bdlwlcogo6/hwt9w3p9/jU1s76JeTgjZS5dhb2z6Uh3XKIPz7ILwe1ymReFonpOA\n"
    "q/M/P69ypG41+4XzigpznWbAaZpA8OdBfCqvtC9f+Kz5y2k2oQ76oRt7xmWCwnsr\n"
    "tIbhq4WFsWMhWjGRElgv8Neh6GPWfR7gbkhjS6tv6ouriIcCAwEAAaNTMFEwHQYD\n"
    "VR0OBBYEFBm/mVUeX4CCCEfE8DckbSM/sitcMB8GA1UdIwQYMBaAFBm/mVUeX4CC\n"
    "CEfE8DckbSM/sitcMA8GA1UdEwEB/wQFMAMBAf8wDQYJKoZIhvcNAQELBQADggEB\n"
    "AAyq6bzlWLcUQfIYA2/uoUZUXdTFNOZ6QwHZyEfOjUHz0qUpgIZklF2kQhfCsnjM\n"
    "q0zOHBZWVBiYMVArnZI/cTT2L9xuLcfgQi4HID3zhoXannLurPTisdOJJe7AhYgD\n"
    "xTXR5FBYvpspt95DJb0ysR8c1Wkt70fOKJ4MPByOoS8UlXes5NJ8LEmHFBEHVoeR\n"
    "kEBOno4Bp8F0WxD0VYCBlMqLABGO71AiF9Y4+3dEKBmWjigssKQHRFFTEjpTLRLb\n"
    "gP02JPfPnOLU9upahwVrmkm9s3fev7VD6MjxxSUkvBf/dcRAMsJYKFgjA8MfU1kA\n"
    "v2jhAAetwz/bLzryeEKyMfM=\n"
    "-----END CERTIFICATE-----\n"
)


def _kubeconfig(clusters: list, contexts: list, current: str | None) -> str:
    doc: dict = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": clusters,
        "contexts": contexts,
        "users": [{"name": "u", "user": {}}],
    }
    if current is not None:
        doc["current-context"] = current
    return yaml.safe_dump(doc)


# ---------------------------------------------------------------------------
# resolve_target：kubeconfig 分支
# ---------------------------------------------------------------------------


def test_resolve_target_from_kubeconfig_current_context():
    # cluster1 = insecure + server1；cluster2 = CA data + server2；current-context → ctx2。
    # 若誤抓第一個，server 會是 server1 且 verify 會是 False。
    kubeconfig = _kubeconfig(
        clusters=[
            {"name": "c1", "cluster": {"server": "https://cluster1.example:6443", "insecure-skip-tls-verify": True}},
            {"name": "c2", "cluster": {"server": "https://cluster2.example:6443", "certificate-authority-data": _CA_B64}},
        ],
        contexts=[
            {"name": "ctx1", "context": {"cluster": "c1", "user": "u"}},
            {"name": "ctx2", "context": {"cluster": "c2", "user": "u"}},
        ],
        current="ctx2",
    )
    target = resolve_target({"kubeconfig": kubeconfig, "in_cluster": "false"})

    assert target.server == "https://cluster2.example:6443"
    assert target.context_name == "ctx2"
    assert isinstance(target.verify, ssl.SSLContext)  # 有 CA → 建了 ssl context，非 False
    assert target.ca_pem is not None and "BEGIN CERTIFICATE" in target.ca_pem


def test_resolve_target_respects_insecure_skip_tls():
    kubeconfig = _kubeconfig(
        clusters=[{"name": "c1", "cluster": {"server": "https://x:6443", "insecure-skip-tls-verify": True}}],
        contexts=[{"name": "ctx1", "context": {"cluster": "c1", "user": "u"}}],
        current="ctx1",
    )
    target = resolve_target({"kubeconfig": kubeconfig, "in_cluster": "false"})

    assert target.verify is False


def test_resolve_target_no_current_context_single_context_ok():
    kubeconfig = _kubeconfig(
        clusters=[{"name": "only", "cluster": {"server": "https://only:6443"}}],
        contexts=[{"name": "only-ctx", "context": {"cluster": "only", "user": "u"}}],
        current=None,
    )
    target = resolve_target({"kubeconfig": kubeconfig, "in_cluster": "false"})

    assert target.context_name == "only-ctx"
    assert target.server == "https://only:6443"


# ---------------------------------------------------------------------------
# resolve_target：in-cluster 分支
# ---------------------------------------------------------------------------


def test_resolve_target_in_cluster(monkeypatch, tmp_path):
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text(_CA_PEM)
    monkeypatch.setattr(cluster_probe, "IN_CLUSTER_CA_PATH", str(ca_file))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")

    target = resolve_target({"in_cluster": "true"})

    assert target.server == "https://10.0.0.1:443"
    assert target.context_name == "in-cluster"
    assert isinstance(target.verify, ssl.SSLContext)

    # env 缺 → fallback https://kubernetes.default.svc
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)
    target2 = resolve_target({"in_cluster": "true"})
    assert target2.server == "https://kubernetes.default.svc"


# ---------------------------------------------------------------------------
# fetch_version
# ---------------------------------------------------------------------------


async def test_fetch_version_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/version"
        return httpx.Response(200, json={"gitVersion": "v1.31.0"})

    target = ProbeTarget(server="https://api:6443", verify=True, context_name="c")
    ok, version, msg = await fetch_version(target, transport=httpx.MockTransport(handler))

    assert ok is True
    assert version == "v1.31.0"


async def test_fetch_version_401_still_reachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    target = ProbeTarget(server="https://api:6443", verify=True, context_name="c")
    ok, version, msg = await fetch_version(target, transport=httpx.MockTransport(handler))

    assert ok is True
    assert version is None
    assert "叢集可達" in msg


async def test_fetch_version_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    target = ProbeTarget(server="https://api:6443", verify=True, context_name="c")
    ok, version, msg = await fetch_version(target, transport=httpx.MockTransport(handler))

    assert ok is False
    assert version is None
    assert msg  # 有具體錯誤訊息
