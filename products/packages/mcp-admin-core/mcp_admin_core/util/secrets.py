"""連線設定 secret 欄位的處理 helper。"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def drop_blank_secrets(
    updates: dict[str, Any], secret_fields: Iterable[str]
) -> dict[str, Any]:
    """回傳 *updates* 的複本，並移除「空白的 secret 欄位」。

    連線表單載入時會把 password 欄清成 ""（masked 值僅作 placeholder），
    因此未變更的 secret 會以 "" 送達。把空白的 secret 欄從 updates 移除，
    交給 ConfigStore.patch()（淺合併）保留既有存檔值＝「留空即沿用現值」。

    非 secret 欄與「非空白的 secret」原樣通過：URL / username 仍可清空，
    真的重打新 secret 也會覆蓋舊值。
    """
    result = dict(updates)
    for field in secret_fields:
        if field in result and not str(result[field]).strip():
            del result[field]
    return result
