"""Spend / cost-reporting tools (category: spend).

Two upstream quirks shape this module and are load-bearing:

1. ``/global/spend/report`` is LiteLLM **Enterprise**-only. On a community
   gateway it answers 400 "You must be a LiteLLM Enterprise user" for every
   parameter combination, so ``litellm_global_spend_report`` is built on
   endpoints the community edition actually serves: client-side aggregation
   over ``/spend/logs`` first, with ``/global/spend/teams`` and
   ``/global/spend/keys`` as fallbacks.
2. ``/spend/logs`` returns ``[]`` when ``api_key`` is combined with a date
   range, even though rows exist inside the window. Both tools therefore query
   by ``api_key`` alone and apply the date window here.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..deps import litellm_client
from ..errors import ToolError
from ..gating import ToolGate
from ..registry import OP_CREATE
from ._common import prune_none, read_only, writing

# group_by value -> the row fields that may carry the bucket identity, most
# specific first. LiteLLM's log rows are not schema-stable across versions, so
# probe a short list rather than betting on one name.
_GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    "team": ("team_id",),
    "api_key": ("api_key", "key_name", "key_alias"),
    "user": ("user_id", "user"),
    "customer": ("end_user", "customer", "end_user_id"),
    "model": ("model_group", "model"),
}

# Where to look when the log fan-out yields nothing for a grouping. These are
# all-time totals (they take no date range), so using one is reported in the
# envelope's `source`/`note` rather than passed off as a windowed answer.
_FALLBACK_ENDPOINTS: dict[str, str] = {
    "team": "/global/spend/teams",
    "api_key": "/global/spend/keys",
}

_NUMERIC_FIELDS = ("spend", "total_tokens", "prompt_tokens", "completion_tokens")


def _row_date(row: dict[str, Any]) -> str:
    """Return a row's ``YYYY-MM-DD`` day, or ``""`` if it has no usable date."""
    for field in ("startTime", "start_time", "date", "created_at"):
        value = row.get(field)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return ""


def _filter_rows_by_date(
    rows: list[dict[str, Any]],
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, Any]]:
    """Keep rows whose day falls inside ``[start_date, end_date]``.

    ``YYYY-MM-DD`` sorts lexicographically, so plain string comparison is
    correct and avoids dragging in timezone-sensitive datetime parsing. Rows
    with no parseable date are kept: dropping them would silently under-report
    spend, which is the exact failure mode this module exists to avoid.
    """
    if not start_date and not end_date:
        return rows
    kept: list[dict[str, Any]] = []
    for row in rows:
        day = _row_date(row)
        if not day:
            kept.append(row)
            continue
        if start_date and day < start_date[:10]:
            continue
        if end_date and day > end_date[:10]:
            continue
        kept.append(row)
    return kept


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """Coerce a LiteLLM spend payload into a list of dict rows."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for field in ("data", "results", "rows", "spend"):
            value = payload.get(field)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _bucket_key(row: dict[str, Any], group_by: str) -> str:
    for field in _GROUP_FIELDS.get(group_by, ()):
        value = row.get(field)
        if value not in (None, ""):
            return str(value)
    return "unattributed"


def _aggregate_spend_rows(
    rows: list[dict[str, Any]], group_by: str
) -> list[dict[str, Any]]:
    """Sum spend/tokens/requests per bucket, biggest spender first."""
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _bucket_key(row, group_by)
        bucket = buckets.setdefault(
            key, {group_by: key, "spend": 0.0, "requests": 0}
        )
        bucket["requests"] += 1
        for field in _NUMERIC_FIELDS:
            value = row.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket[field] = bucket.get(field, 0) + value
        # Carry a human label through when the row offers one.
        for label in ("team_alias", "key_alias", "user_email", "model"):
            if row.get(label) and label not in bucket:
                bucket[label] = row[label]
    return sorted(
        buckets.values(), key=lambda b: float(b.get("spend") or 0.0), reverse=True
    )


async def _get_logs(ctx: Context, params: dict[str, Any]) -> Any:
    """GET /spend/logs with ``params`` (``summarize`` stays a real boolean)."""
    return await litellm_client(ctx).get("/spend/logs", params=params)


def register(mcp: Any, gate: ToolGate) -> None:
    if gate.is_tool_enabled("litellm_spend_logs"):

        @mcp.tool(
            name="litellm_spend_logs",
            annotations=read_only("Spend logs"),
        )
        async def litellm_spend_logs(
            ctx: Context,
            api_key: str | None = None,
            user_id: str | None = None,
            request_id: str | None = None,
            start_date: str | None = None,
            end_date: str | None = None,
            summarize: bool = False,
        ) -> Any:
            """Fetch spend logs. Dates are ``YYYY-MM-DD`` (inclusive).

            TWO RESPONSE SHAPES, chosen by ``summarize``:

            * ``summarize=False`` (the default here) returns PER-REQUEST rows —
              ``request_id``, ``startTime``, ``model``, ``prompt_tokens``,
              ``spend``, ``api_key``, ``user``…
            * ``summarize=True`` returns ONE AGGREGATE OBJECT PER DAY, e.g.
              ``{"startTime": "2026-08-04", "spend": 0.0046, "users": {...},
              "models": {...}, "<hashed-api-key>": 0.0045}``. Note the hashed
              API keys sit alongside the fixed keys at the same level — do not
              read them as field names.

            LiteLLM's own default is ``summarize=True``; this tool pins it to
            ``False`` so the documented "rows" shape is what callers get.

            Filter by ``api_key``, ``user_id`` or a single ``request_id``.
            """
            params = prune_none(
                {
                    "api_key": api_key,
                    "user_id": user_id,
                    "request_id": request_id,
                    "start_date": start_date,
                    "end_date": end_date,
                    "summarize": summarize,
                }
            )
            # Upstream bug: api_key + a date range returns [] even when rows
            # exist in the window (user_id + dates is fine). Returning that
            # empty list reads as "this key spent nothing" — a wrong answer
            # dressed as success. Query by key alone and window the rows here.
            client_side_window = bool(api_key and (start_date or end_date))
            if client_side_window:
                params.pop("start_date", None)
                params.pop("end_date", None)
                # Day filtering needs per-request rows; the daily aggregate has
                # already collapsed them.
                params["summarize"] = False

            result = await _get_logs(ctx, params)

            if client_side_window:
                rows = _filter_rows_by_date(
                    _as_rows(result), start_date, end_date
                )
                if summarize:
                    return {
                        "rows": rows,
                        "note": (
                            "summarize=True is not available when api_key is "
                            "combined with a date range (LiteLLM returns no "
                            "rows for that combination), so per-request rows "
                            "filtered client-side are returned instead."
                        ),
                    }
                return rows
            return result

    if gate.is_tool_enabled("litellm_global_spend_report"):

        @mcp.tool(
            name="litellm_global_spend_report",
            annotations=read_only("Global spend report"),
        )
        async def litellm_global_spend_report(
            ctx: Context,
            start_date: str,
            end_date: str,
            group_by: str = "team",
            api_key: str | None = None,
            internal_user_id: str | None = None,
            team_id: str | None = None,
        ) -> dict:
            """Aggregated spend over a date range, grouped and totalled.

            ``group_by`` is ``team``/``api_key``/``user``/``customer``/``model``;
            dates are required and formatted ``YYYY-MM-DD`` (inclusive).

            Returns ``{group_by, start_date, end_date, source, rows,
            total_spend}`` where each row is
            ``{<group_by>: <id>, spend, requests, total_tokens, ...}``.

            This does NOT call ``/global/spend/report``: that endpoint is
            LiteLLM Enterprise-only and 400s on a community gateway for every
            parameter combination. Spend is aggregated from ``/spend/logs``
            instead, with the all-time ``/global/spend/{teams,keys}`` endpoints
            as a fallback (flagged in ``source`` when used).
            """
            group = (group_by or "team").strip().lower()
            if group not in _GROUP_FIELDS:
                raise ToolError(
                    f"group_by='{group_by}' is not supported. Choose one of: "
                    f"{sorted(_GROUP_FIELDS)}."
                )

            params: dict[str, Any] = {"summarize": False}
            # Same upstream bug as litellm_spend_logs: never send api_key
            # together with dates, or the gateway answers [].
            if api_key:
                params["api_key"] = api_key
            else:
                params["start_date"] = start_date
                params["end_date"] = end_date
            if internal_user_id:
                params["user_id"] = internal_user_id

            raw = await _get_logs(ctx, params)
            rows = _filter_rows_by_date(_as_rows(raw), start_date, end_date)
            if team_id:
                rows = [r for r in rows if str(r.get("team_id") or "") == team_id]

            source = "/spend/logs"
            note: str | None = None
            aggregated = _aggregate_spend_rows(rows, group)

            if not aggregated and not (api_key or internal_user_id or team_id):
                # No log rows survived (retention window, or logging disabled).
                # The /global/spend/* endpoints keep running totals, so they can
                # still answer — but they ignore the date range, so say so.
                fallback = _FALLBACK_ENDPOINTS.get(group)
                if fallback:
                    fallback_rows = _as_rows(
                        await litellm_client(ctx).get(fallback)
                    )
                    if fallback_rows:
                        aggregated = _aggregate_spend_rows(fallback_rows, group)
                        source = fallback
                        note = (
                            f"No /spend/logs rows in {start_date}..{end_date}; "
                            f"showing ALL-TIME totals from {fallback}. The date "
                            f"range was not applied to these numbers."
                        )

            if not aggregated:
                note = (
                    f"No spend recorded for group_by='{group}' between "
                    f"{start_date} and {end_date}. If you expected data, widen "
                    f"the range or check that LiteLLM request logging is on "
                    f"(the Enterprise /global/spend/report endpoint is not "
                    f"available on this gateway, so this report is built from "
                    f"/spend/logs)."
                )

            report: dict[str, Any] = {
                "group_by": group,
                "start_date": start_date,
                "end_date": end_date,
                "source": source,
                "rows": aggregated,
                "total_spend": round(
                    sum(float(r.get("spend") or 0.0) for r in aggregated), 10
                ),
            }
            if note:
                report["note"] = note
            return report

    if gate.is_tool_enabled("litellm_spend_calculate"):

        @mcp.tool(
            name="litellm_spend_calculate",
            annotations=writing("Spend calculate"),
        )
        async def litellm_spend_calculate(
            ctx: Context,
            model: str | None = None,
            messages: list[dict[str, Any]] | None = None,
            completion_response: dict[str, Any] | None = None,
        ) -> dict:
            """Estimate cost without billing.

            Tokenization can fetch remote images or tokenizer resources.
            Requires active-operation permission; this does not run inference.

            Provide either ``model`` + ``messages`` (to price a hypothetical
            request) or a ``completion_response`` (to price a past one).

            Use the PROVIDER-PREFIXED model name (``openai/gpt-4o-mini``, not
            the gateway alias ``gpt-4o-mini``): LiteLLM prices from its cost map,
            which is keyed by provider model, and an alias it cannot resolve is
            priced at 0 rather than rejected.
            """
            gate.require_operation("litellm_spend_calculate", OP_CREATE)
            body = prune_none(
                {
                    "model": model,
                    "messages": messages,
                    "completion_response": completion_response,
                }
            )
            result = await litellm_client(ctx).post(
                "/spend/calculate", json_data=body
            )
            # A bare {"cost": 0} is indistinguishable from "this model is free".
            # It almost always means the alias missed the cost map, so annotate
            # rather than hand back a silently wrong zero.
            if (
                isinstance(result, dict)
                and model
                and "/" not in model
                and not float(result.get("cost") or 0.0)
            ):
                result = {
                    **result,
                    "warning": (
                        f"LiteLLM priced this at 0. '{model}' looks like a "
                        f"gateway alias with no cost-map entry rather than a "
                        f"free model — retry with the provider-prefixed name "
                        f"(e.g. 'openai/{model}') to get a real estimate."
                    ),
                }
            return result
