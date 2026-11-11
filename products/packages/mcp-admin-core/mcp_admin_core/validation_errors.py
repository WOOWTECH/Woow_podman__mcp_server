"""422 回應不回顯送出的值。

FastAPI 預設的 ``RequestValidationError`` 處理把 pydantic 的 ``errors()`` 原樣放進 ``detail``，每一筆都帶
``input``（送出的原始值；必填欄位缺漏時是整個 body）。連線設定的 PUT 帶密碼與 API key：欄位名拼錯
（產品的連線模型是 ``extra='forbid'``）、缺欄位或型別不對時，預設的 422 會把憑證原樣送回呼叫端，留在瀏覽器
開發者工具、反向代理與 AI 代理的對話紀錄裡。pydantic 的 ``hide_input_in_errors`` 只影響
``str(ValidationError)``，``errors()`` 照樣帶 ``input``，所以由 ``create_app`` 統一換成這個處理：狀態碼與
``detail`` 的形狀照舊（``type``／``loc``／``msg``／``ctx``），只拿掉 ``input``。

app 自己另外註冊的處理（例如 EMQX 回固定錯誤碼）在 ``create_app`` 之後註冊，會蓋過這個。
"""

from __future__ import annotations

from typing import Any, Iterable

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request


def errors_without_input(errors: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in error.items() if key != "input"} for error in errors]


async def validation_error_without_input(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors_without_input(exc.errors()))})
