"""Spend tools build the right requests and parse into the response models.

Replaces the reference repo's ``test_retained_topics``: the spend family is the
LiteLLM analogue of a read-heavy, filter-driven query surface.
"""

from __future__ import annotations

import pytest

from woow_litellm_mcp_server.errors import ToolError
from woow_litellm_mcp_server.models import SpendRow
from woow_litellm_mcp_server.tools import spend as spend_module

from .conftest import MockLiteLLM, build_gate, make_ctx


def _spend_tools():
    from .conftest import FakeMCP

    mcp = FakeMCP()
    spend_module.register(mcp, build_gate())
    return mcp.tools


async def test_spend_logs_defaults_to_per_request_rows() -> None:
    """summarize is pinned to False so the documented "rows" shape is returned.

    LiteLLM's own default is summarize=True (daily aggregate objects), a
    completely different shape from the one the docstring promises.
    """
    rows = [
        {
            "request_id": "req-1",
            "api_key": "sk-abc",
            "user": "u1",
            "model": "gpt-4o",
            "spend": 0.0123,
            "total_tokens": 542,
            "startTime": "2026-08-01T00:00:00Z",
        }
    ]
    gw = MockLiteLLM().route("/spend/logs", rows)
    client = gw.client()
    try:
        tool = _spend_tools()["litellm_spend_logs"]
        result = await tool(
            make_ctx(client),
            user_id="u1",
            start_date="2026-08-01",
            end_date="2026-08-02",
        )
    finally:
        await client.aclose()

    assert gw.last.method == "GET"
    assert gw.last.url.path == "/spend/logs"
    params = gw.last_params()
    # user_id + dates is the combination LiteLLM handles correctly: forwarded
    # verbatim, with the explicit summarize=false that pins the response shape.
    assert params["user_id"] == "u1"
    assert params["start_date"] == "2026-08-01"
    assert params["end_date"] == "2026-08-02"
    assert params["summarize"] == "false"
    assert "api_key" not in params
    assert "request_id" not in params

    parsed = [SpendRow.model_validate(r) for r in result]
    assert parsed[0].request_id == "req-1"
    assert parsed[0].spend == 0.0123
    assert parsed[0].total_tokens == 542


async def test_spend_logs_windows_api_key_dates_client_side() -> None:
    """api_key + dates must not be sent together: upstream returns [].

    Regression guard for the worst failure class here — a well-formed empty
    result that reads as "this key spent nothing" while rows exist.
    """
    rows = [
        {"request_id": "in-1", "api_key": "sk-abc", "spend": 1.0,
         "startTime": "2026-08-05T09:00:00Z"},
        {"request_id": "out-early", "api_key": "sk-abc", "spend": 2.0,
         "startTime": "2026-07-31T23:59:00Z"},
        {"request_id": "out-late", "api_key": "sk-abc", "spend": 4.0,
         "startTime": "2026-08-06T00:00:01Z"},
    ]
    gw = MockLiteLLM().route("/spend/logs", rows)
    client = gw.client()
    try:
        tool = _spend_tools()["litellm_spend_logs"]
        result = await tool(
            make_ctx(client),
            api_key="sk-abc",
            start_date="2026-08-01",
            end_date="2026-08-05",
        )
    finally:
        await client.aclose()

    params = gw.last_params()
    assert params["api_key"] == "sk-abc"
    assert "start_date" not in params, "dates must be dropped from the api_key query"
    assert "end_date" not in params
    # The window is applied here instead, inclusively on both ends.
    assert [r["request_id"] for r in result] == ["in-1"]


async def test_global_spend_report_avoids_the_enterprise_endpoint() -> None:
    """/global/spend/report is Enterprise-only and 400s on this gateway."""
    logs = [
        {"team_id": "team-a", "spend": 1.5, "total_tokens": 10,
         "startTime": "2026-08-01T10:00:00Z"},
        {"team_id": "team-a", "spend": 2.0, "total_tokens": 20,
         "startTime": "2026-08-02T10:00:00Z"},
        {"team_id": "team-b", "spend": 0.5, "total_tokens": 5,
         "startTime": "2026-08-03T10:00:00Z"},
        # Outside the window: must not be counted.
        {"team_id": "team-a", "spend": 99.0, "total_tokens": 1,
         "startTime": "2026-09-01T10:00:00Z"},
    ]
    gw = MockLiteLLM().route("/spend/logs", logs)
    client = gw.client()
    try:
        tool = _spend_tools()["litellm_global_spend_report"]
        result = await tool(
            make_ctx(client),
            start_date="2026-08-01",
            end_date="2026-08-31",
            group_by="team",
        )
    finally:
        await client.aclose()

    assert gw.last.url.path == "/spend/logs"
    assert all(r.url.path != "/global/spend/report" for r in gw.requests)
    assert result["group_by"] == "team"
    assert result["source"] == "/spend/logs"
    assert result["total_spend"] == 4.0
    # Biggest spender first.
    assert result["rows"][0]["team"] == "team-a"
    assert result["rows"][0]["spend"] == 3.5
    assert result["rows"][0]["requests"] == 2
    assert "note" not in result


async def test_global_spend_report_falls_back_when_logs_are_empty() -> None:
    """With no log rows the tool still answers, and labels the source."""
    gw = (
        MockLiteLLM()
        .route("/spend/logs", [])
        .route(
            "/global/spend/teams",
            [{"team_id": "team-a", "spend": 7.5, "team_alias": "Alpha"}],
        )
    )
    client = gw.client()
    try:
        tool = _spend_tools()["litellm_global_spend_report"]
        result = await tool(
            make_ctx(client),
            start_date="2026-08-01",
            end_date="2026-08-31",
            group_by="team",
        )
    finally:
        await client.aclose()

    assert result["source"] == "/global/spend/teams"
    assert result["total_spend"] == 7.5
    assert result["rows"][0]["team_alias"] == "Alpha"
    # The fallback endpoint ignores dates, so that must be stated, not implied.
    assert "ALL-TIME" in result["note"]


async def test_global_spend_report_rejects_an_unknown_group_by() -> None:
    gw = MockLiteLLM().route("/spend/logs", [])
    client = gw.client()
    try:
        tool = _spend_tools()["litellm_global_spend_report"]
        with pytest.raises(ToolError):
            await tool(
                make_ctx(client),
                start_date="2026-08-01",
                end_date="2026-08-31",
                group_by="galaxy",
            )
    finally:
        await client.aclose()


async def test_spend_calculate_posts_body() -> None:
    gw = MockLiteLLM().route("/spend/calculate", {"cost": 0.004})
    client = gw.client()
    try:
        tool = _spend_tools()["litellm_spend_calculate"]
        result = await tool(
            make_ctx(client),
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
    finally:
        await client.aclose()

    assert gw.last.method == "POST"
    assert gw.last.url.path == "/spend/calculate"
    body = gw.last_json()
    assert body["model"] == "gpt-4o"
    assert body["messages"][0]["content"] == "hi"
    # completion_response was None -> pruned from the body.
    assert "completion_response" not in body
    assert result == {"cost": 0.004}


async def test_spend_calculate_annotates_a_zero_cost_alias() -> None:
    """cost==0 for an unprefixed alias is a missed cost-map entry, not "free"."""
    gw = MockLiteLLM().route("/spend/calculate", {"cost": 0})
    client = gw.client()
    try:
        tool = _spend_tools()["litellm_spend_calculate"]
        aliased = await tool(
            make_ctx(client),
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
    finally:
        await client.aclose()
    assert aliased["cost"] == 0
    assert "openai/gpt-4o-mini" in aliased["warning"]

    # A provider-prefixed name that genuinely prices at 0 is left alone.
    gw2 = MockLiteLLM().route("/spend/calculate", {"cost": 0})
    client2 = gw2.client()
    try:
        tool = _spend_tools()["litellm_spend_calculate"]
        prefixed = await tool(
            make_ctx(client2),
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
    finally:
        await client2.aclose()
    assert "warning" not in prefixed


def test_spend_model_is_permissive_to_new_fields() -> None:
    # extra="allow": a newer LiteLLM field must not break parsing.
    row = SpendRow.model_validate(
        {"request_id": "r", "spend": 0.1, "some_new_litellm_field": 42}
    )
    assert row.request_id == "r"
    assert row.model_dump().get("some_new_litellm_field") == 42
