"""參數化的祕密遮罩 helper（取代各 router 重複的 _mask/_mask_key）。"""
from __future__ import annotations


def mask_secret(
    value: str,
    *,
    prefix: int = 4,
    suffix: int = 4,
    min_len: int = 8,
    max_stars: int | None = None,
) -> str:
    """遮罩祕密字串，只露出前 ``prefix`` 與後 ``suffix`` 個字元。

    - ``value`` 為空 → 回 ""（呼叫端通常自行決定空值替代字串）。
    - ``len(value) <= min_len`` → 回 "****"。
    - 否則回 ``value[:prefix] + '*'*n + value[-suffix:]``，其中 n 為中間長度；
      若指定 ``max_stars`` 則星號數上限為該值（保留 tokens canon 行為）。
    """
    if not value:
        return ""
    if len(value) <= min_len:
        return "****"
    n = len(value) - prefix - suffix
    if max_stars is not None:
        n = min(n, max_stars)
    return f"{value[:prefix]}{'*' * n}{value[-suffix:]}"
