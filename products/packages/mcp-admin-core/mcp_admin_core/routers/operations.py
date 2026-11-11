"""Current-admin-only observation of durable operation receipts."""
import asyncio

from fastapi import APIRouter, Request, Response

from ..config import get_config_store
from ..configuration import _check_queued_session
from ..operations import OperationJournal

router = APIRouter(prefix="/api/operations", tags=["operations"])


@router.get("/{operation_id}")
async def get_operation(operation_id: str, request: Request, response: Response):
    store = get_config_store()
    async with store.transition():
        await _check_queued_session(request, await store.load())
        result = await asyncio.to_thread(OperationJournal(store._path).query, operation_id)
        response.headers["Cache-Control"] = "no-store"
        return result
