"""閘道請求內容位元組預算的預設值要扣掉「帳號數 × 子行程記憶體」（第二輪 P1-B；審查 F2、沙盒 M2C／M4 的 OOM 與薄餘裕）。

第一輪的預設值只看 cgroup 上限（1 GiB → 全域 83.8 MiB／每帳號 41.9 MiB），沒有扣掉子行程：沙盒實測 Odoo 6 帳號、兩個帳號各自
塞滿預算時 OOMKilled 一次（peak 1013 MiB）、第二次只剩 44 MiB；n8n 2 帳號餘裕 71～89 MiB。

新公式：全域 ＝ (cgroup 上限 − 閘道基線 − 最大帳號數 × 子行程記憶體 − 安全邊際) ÷ 放大倍數，每帳號 ＝ 全域的一半，兩者至少 16 MiB。
帳號數與子行程記憶體來自產品的 LaunchPolicy 與 ``profiles.current_profile_capacity``（與帳號容量判斷同一份）。
"""
from __future__ import annotations

import pytest

from mcp_admin_core import request_budget as rb
from mcp_admin_core.launch_policy import LaunchPolicy, get_launch_policy, set_launch_policy

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _restore_launch_policy():
    """``set_launch_policy`` 是全域狀態：測試結束一律還原，不讓後面的測試看到這裡設的政策（第二輪審查 F8）。"""
    before = get_launch_policy()
    yield
    set_launch_policy(before)


def _policy(max_extra: int, child_mib: int) -> LaunchPolicy:
    return LaunchPolicy(command="x", max_extra_profiles=max_extra, profile_child_memory_mib=child_mib)


def _limits(monkeypatch, memory_mib, policy):
    set_launch_policy(policy)
    monkeypatch.setattr(rb, "_memory_limit_mib", lambda: memory_mib)
    monkeypatch.setattr("mcp_admin_core.profiles._memory_limit_mib", lambda: memory_mib)
    return rb.BudgetLimits.from_env()


def _headroom(memory_mib, accounts, real_child_mib, limits, amplification):
    """預算全用滿、所有帳號的子行程都在跑時，離上限還剩多少 MiB。"""
    return memory_mib - (rb.GATEWAY_BASELINE_MIB + accounts * real_child_mib + limits.total / MIB * amplification)


# 沙盒第七輪實測的場景：(產品, 帳號上限, 政策的子行程估計, 實測子行程 RSS, 實測放大倍數)
SCENARIOS = {
    "odoo-6-accounts": (_policy(5, 100), 6, 92, 6.8),
    "hermes-4-accounts": (_policy(3, 160), 4, 92, 6.8),
    "n8n-2-accounts": (_policy(2, 300), 2, 270, 8.3),  # n8n 子行程吃到 4 MiB 請求後約 260～270 MiB
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_one_gib_pod_keeps_at_least_200_mib_of_headroom_in_the_sandbox_worst_cases(monkeypatch, name):
    policy, accounts, real_child, real_amp = SCENARIOS[name]
    limits = _limits(monkeypatch, 1024, policy)
    assert rb.runtime_inputs(1024)[0] == accounts
    assert _headroom(1024, accounts, real_child, limits, real_amp) >= 200, name
    # 用政策自己的估計與本模組的放大倍數（保守）算，也不低於安全邊際
    assert _headroom(1024, accounts, policy.profile_child_memory_mib, limits, rb.AMPLIFICATION) >= rb.SAFETY_MARGIN_MIB - 1 \
        or limits.total == rb.MIN_BUDGET_BYTES
    # 單一最大的合法請求（Nextcloud 13.3 MiB 上傳、16 MiB 上限）永遠放得進去
    assert limits.per_profile >= 16 * MIB and limits.total >= 16 * MIB


def test_old_defaults_would_have_failed_the_sandbox_scenarios(monkeypatch):
    """對照：第一輪的 83.8 MiB 全域預算在 Odoo 6 帳號的實測放大下，餘裕只有 ~44 MiB（沙盒第二次 980 MiB）。"""
    policy, accounts, real_child, real_amp = SCENARIOS["odoo-6-accounts"]
    old = rb.BudgetLimits(total=int(83.8 * MIB), per_profile=int(41.9 * MIB))
    assert _headroom(1024, accounts, real_child, old, real_amp) < 100


def test_more_accounts_or_heavier_children_shrink_the_budget(monkeypatch):
    few = _limits(monkeypatch, 2048, _policy(1, 100)).total
    many = _limits(monkeypatch, 2048, _policy(5, 100)).total
    heavy = _limits(monkeypatch, 2048, _policy(5, 300)).total
    assert few > many > heavy >= 16 * MIB


def test_formula_matches_the_documented_arithmetic(monkeypatch):
    limits = _limits(monkeypatch, 2048, _policy(5, 100))  # 2 GiB、Odoo：6 個帳號
    expected = int((2048 - rb.GATEWAY_BASELINE_MIB - 6 * 100 - rb.SAFETY_MARGIN_MIB) * MIB / rb.AMPLIFICATION)
    assert limits.total == expected and limits.per_profile == max(16 * MIB, expected // 2)


def test_small_pod_is_clamped_to_the_single_request_ceiling(monkeypatch):
    limits = _limits(monkeypatch, 512, _policy(5, 100))
    assert limits.total == 16 * MIB and limits.per_profile == 16 * MIB


def test_environment_variables_still_win(monkeypatch):
    monkeypatch.setenv(rb.TOTAL_ENV, str(100 * MIB))
    monkeypatch.setenv(rb.PER_PROFILE_ENV, str(50 * MIB))
    limits = _limits(monkeypatch, 1024, _policy(5, 100))
    assert (limits.total, limits.per_profile) == (100 * MIB, 50 * MIB)


def test_amplification_covers_the_measured_worst_case_under_the_structure_guard():
    """16 MiB 最壞 body（守門上限內的結構＋大字串）實測 8.0 倍（round2/amp_rss_after.json）；預算假設不低於它。"""
    assert rb.AMPLIFICATION >= 8.0
