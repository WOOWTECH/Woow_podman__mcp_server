"""mask_secret 參數化遮罩 helper 測試（涵蓋四種既有呼叫者的參數）。"""
from __future__ import annotations

from mcp_admin_core.util.mask import mask_secret


def test_empty_returns_empty():
    assert mask_secret("") == ""


def test_below_or_equal_min_len_returns_stars():
    assert mask_secret("short", min_len=8) == "****"          # len 5 <= 8
    assert mask_secret("ab", prefix=1, suffix=1, min_len=2) == "****"  # odoo config


def test_default_first4_last4():
    # config hermes/n8n: prefix4/suffix4/min8
    assert mask_secret("abcdefghijkl") == "abcd****ijkl"      # 12 chars -> 4 stars


def test_settings_first2_last2():
    # settings.py: prefix2/suffix2/min4
    assert mask_secret("abcdef", prefix=2, suffix=2, min_len=4) == "ab**ef"


def test_token_cap_max_stars():
    # tokens canon: prefix4/suffix4/min8, 星號上限 20
    token = "a" * 64
    out = mask_secret(token, prefix=4, suffix=4, min_len=8, max_stars=20)
    assert out == "aaaa" + "*" * 20 + "aaaa"
    assert out.count("*") == 20
