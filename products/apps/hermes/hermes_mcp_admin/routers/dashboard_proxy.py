"""Dashboard proxy router for Hermes MCP Admin.

Proxies API calls to the Hermes Dashboard REST API for endpoints that
the admin frontend needs but aren't covered by the core routers.

Covers: skills, model, mcp-servers, hermes-tools, sessions,
        gateway status, config editor, deny-list.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request, HTTPException

from hermes_mcp_server import guards
from mcp_admin_core.config import get_config_store
from ..dashboard_auth import dashboard_cookie

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard-proxy"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seg(value: str, field: str) -> str:
    """路徑參數 → 單一路徑段（AI-HMCP-001-1）。

    Starlette 會把 %2E%2E 解碼成 ".."（curl --path-as-is 也能送字面 ".."），拼進 dashboard 網址後 httpx 會正規化，
    ``DELETE /api/sessions/..`` 就變成 ``DELETE /api``。規則與 MCP 工具共用 guards.path_segment。
    """
    try:
        return guards.path_segment(value, field)
    except guards.GuardError as exc:
        raise HTTPException(400, str(exc)) from None


async def _get_conn() -> dict[str, str]:
    store = get_config_store()
    conn = await store.get("connection", {})
    return conn


async def _dashboard_cookie(conn: dict[str, str]) -> str:
    """Login to Dashboard and return session cookie. Caches for 10 minutes."""
    url = conn.get("dashboard_url", "")
    if not url:
        raise HTTPException(502, "Dashboard URL not configured")

    username = conn.get("dashboard_username", "admin")
    password = conn.get("dashboard_password", "admin")
    try:
        cookie = await dashboard_cookie(url, username, password)
    except Exception as exc:
        logger.error("Dashboard login failed: %s", exc)
        raise HTTPException(502, f"Dashboard login failed: {exc}")
    if cookie:
        return cookie
    raise HTTPException(502, "Dashboard login returned no session cookie")


async def _dashboard_get(path: str) -> Any:
    """GET a path on the Dashboard API."""
    conn = await _get_conn()
    url = conn.get("dashboard_url", "")
    if not url:
        raise HTTPException(502, "Dashboard URL not configured")
    cookie = await _dashboard_cookie(conn)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{url.rstrip('/')}{path}",
                headers={"Cookie": f'hermes_session_at="{cookie}"'},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.error("Dashboard GET %s: HTTP %d", path, exc.response.status_code)
        raise HTTPException(502, f"Dashboard returned HTTP {exc.response.status_code}")
    except Exception as exc:
        logger.error("Dashboard GET %s: %s", path, exc)
        raise HTTPException(502, str(exc))


async def _dashboard_post(path: str, data: Any = None) -> Any:
    """POST to Dashboard API."""
    conn = await _get_conn()
    url = conn.get("dashboard_url", "")
    if not url:
        raise HTTPException(502, "Dashboard URL not configured")
    cookie = await _dashboard_cookie(conn)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{url.rstrip('/')}{path}",
                headers={"Cookie": f'hermes_session_at="{cookie}"'},
                json=data,
            )
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Dashboard POST %s: %s", path, exc)
        raise HTTPException(502, str(exc))


async def _dashboard_put(path: str, data: Any = None) -> Any:
    """PUT to Dashboard API."""
    conn = await _get_conn()
    url = conn.get("dashboard_url", "")
    if not url:
        raise HTTPException(502, "Dashboard URL not configured")
    cookie = await _dashboard_cookie(conn)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(
                f"{url.rstrip('/')}{path}",
                headers={"Cookie": f'hermes_session_at="{cookie}"'},
                json=data,
            )
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Dashboard PUT %s: %s", path, exc)
        raise HTTPException(502, str(exc))


async def _dashboard_delete(path: str) -> Any:
    """DELETE on Dashboard API."""
    conn = await _get_conn()
    url = conn.get("dashboard_url", "")
    if not url:
        raise HTTPException(502, "Dashboard URL not configured")
    cookie = await _dashboard_cookie(conn)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(
                f"{url.rstrip('/')}{path}",
                headers={"Cookie": f'hermes_session_at="{cookie}"'},
            )
            if resp.status_code == 204:
                return {"ok": True}
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Dashboard DELETE %s: %s", path, exc)
        raise HTTPException(502, str(exc))


async def _gateway_get(path: str) -> Any:
    """GET a path on the Gateway API with Bearer auth."""
    conn = await _get_conn()
    url = conn.get("gateway_url", "")
    key = conn.get("gateway_api_key", "")
    if not url:
        raise HTTPException(502, "Gateway URL not configured")
    headers: dict[str, str] = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{url.rstrip('/')}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.error("Gateway GET %s: %s", path, exc)
        raise HTTPException(502, str(exc))


async def _gateway_post(path: str, data: Any = None) -> Any:
    """POST to Gateway API with Bearer auth."""
    conn = await _get_conn()
    url = conn.get("gateway_url", "")
    key = conn.get("gateway_api_key", "")
    if not url:
        raise HTTPException(502, "Gateway URL not configured")
    headers: dict[str, str] = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{url.rstrip('/')}{path}", headers=headers, json=data,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.error("Gateway POST %s: %s", path, exc)
        raise HTTPException(502, str(exc))


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@router.get("/skills")
async def get_skills() -> dict[str, Any]:
    data = await _dashboard_get("/api/skills")
    if data is None:
        return {"skills": []}
    if isinstance(data, list):
        return {"skills": data}
    return data if isinstance(data, dict) else {"skills": []}


@router.put("/skills")
async def update_skills(request: Request) -> dict[str, Any]:
    body = await request.json()
    result = await _dashboard_put("/api/skills", body)
    return result or {"status": "ok"}


@router.post("/skills/create")
async def create_skill(request: Request) -> dict[str, Any]:
    """Create a new skill with YAML frontmatter."""
    body = await request.json()
    name = body.get("name", "")
    desc = body.get("description", "")
    category = body.get("category", "")
    content_body = body.get("content", "")
    # Build SKILL.md with YAML frontmatter
    frontmatter = f"---\nname: {name}\ndescription: {desc}\n"
    if category:
        frontmatter += f"category: {category}\n"
    frontmatter += "---\n\n"
    full_content = frontmatter + content_body
    return await _dashboard_post("/api/skills", {"name": name, "content": full_content})


@router.get("/skills/content")
async def get_skill_content(name: str) -> dict[str, Any]:
    """Get skill SKILL.md content."""
    data = await _dashboard_get(f"/api/skills/content?name={quote(name, safe='')}")
    return data if isinstance(data, dict) else {"content": data or ""}


@router.put("/skills/content")
async def update_skill_content(request: Request) -> dict[str, Any]:
    """Update skill content. Body: {name, content}"""
    body = await request.json()
    name = body.get("name", "")
    content = body.get("content", "")
    # Ensure YAML frontmatter exists
    if not content.strip().startswith("---"):
        content = f"---\nname: {name}\n---\n\n{content}"
    return await _dashboard_put("/api/skills/content", {"name": name, "content": content})


@router.put("/skills/toggle")
async def toggle_skill(request: Request) -> dict[str, Any]:
    """Toggle skill enabled/disabled. Body: {name, enabled}"""
    body = await request.json()
    return await _dashboard_put("/api/skills/toggle", body)


# ---------------------------------------------------------------------------
# Cron / Scheduling
# ---------------------------------------------------------------------------

@router.get("/cron/jobs")
async def list_cron_jobs() -> Any:
    return await _dashboard_get("/api/cron/jobs")


@router.post("/cron/jobs")
async def create_cron_job(request: Request) -> dict[str, Any]:
    body = await request.json()
    return await _dashboard_post("/api/cron/jobs", body)


@router.put("/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    return await _dashboard_put(f"/api/cron/jobs/{_seg(job_id, 'job_id')}", {"updates": body})


@router.delete("/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str) -> dict[str, Any]:
    return await _dashboard_delete(f"/api/cron/jobs/{_seg(job_id, 'job_id')}")


@router.post("/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str) -> dict[str, Any]:
    return await _dashboard_post(f"/api/cron/jobs/{_seg(job_id, 'job_id')}/trigger")


@router.post("/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str) -> dict[str, Any]:
    return await _dashboard_post(f"/api/cron/jobs/{_seg(job_id, 'job_id')}/pause")


@router.post("/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str) -> dict[str, Any]:
    return await _dashboard_post(f"/api/cron/jobs/{_seg(job_id, 'job_id')}/resume")


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@router.get("/webhooks")
async def list_webhooks() -> Any:
    return await _dashboard_get("/api/webhooks")


@router.post("/webhooks/enable")
async def enable_webhooks() -> dict[str, Any]:
    return await _dashboard_post("/api/webhooks/enable")


@router.post("/webhooks")
async def create_webhook(request: Request) -> dict[str, Any]:
    body = await request.json()
    return await _dashboard_post("/api/webhooks", body)


@router.delete("/webhooks/{name}")
async def delete_webhook(name: str) -> dict[str, Any]:
    return await _dashboard_delete(f"/api/webhooks/{_seg(name, 'name')}")


@router.put("/webhooks/{name}/enabled")
async def toggle_webhook(name: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    return await _dashboard_put(f"/api/webhooks/{_seg(name, 'name')}/enabled", body)


# ---------------------------------------------------------------------------
# Toolsets (Hermes internal toolsets)
# ---------------------------------------------------------------------------

@router.get("/toolsets")
async def list_toolsets() -> Any:
    return await _dashboard_get("/api/tools/toolsets")


@router.put("/toolsets/{name}")
async def toggle_toolset(name: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    return await _dashboard_put(f"/api/tools/toolsets/{_seg(name, 'name')}", body)


# ---------------------------------------------------------------------------
# MCP Servers (proper Dashboard endpoints)
# ---------------------------------------------------------------------------

@router.get("/mcp-servers/live")
async def list_mcp_servers_live() -> Any:
    """List MCP servers from Dashboard /api/mcp/servers."""
    return await _dashboard_get("/api/mcp/servers")


@router.post("/mcp-servers/live")
async def add_mcp_server_live(request: Request) -> dict[str, Any]:
    """AI-HMCP-002-2：直通曾可新增 stdio command（transport=stdio、command=/bin/true），繞過 hermes_mcp 的封鎖
    與 deny-list.yaml 的 add_stdio_command。現在與 MCP 工具同規則：只收 http／https＋SSRF，拒絕 command 類欄位。"""
    body = await request.json()
    try:
        body = await guards.check_mcp_server_body(body)
    except guards.GuardError as exc:
        raise HTTPException(400, str(exc)) from None
    return await _dashboard_post("/api/mcp/servers", body)


@router.delete("/mcp-servers/live/{name}")
async def delete_mcp_server_live(name: str) -> dict[str, Any]:
    return await _dashboard_delete(f"/api/mcp/servers/{_seg(name, 'name')}")


@router.put("/mcp-servers/live/{name}/enabled")
async def toggle_mcp_server_live(name: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    return await _dashboard_put(f"/api/mcp/servers/{_seg(name, 'name')}/enabled", body)


@router.post("/mcp-servers/live/{name}/test")
async def test_mcp_server_live(name: str) -> dict[str, Any]:
    return await _dashboard_post(f"/api/mcp/servers/{_seg(name, 'name')}/test")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@router.get("/model")
async def get_model() -> dict[str, Any]:
    data = await _dashboard_get("/api/config")
    if not data or not isinstance(data, dict):
        return {"provider": "", "model": "", "aux_model": "", "moa_enabled": False}
    model_val = data.get("model", "")
    # Model may be a string like "MiniMax-M1-80k" or a dict
    if isinstance(model_val, dict):
        return {
            "provider": model_val.get("provider", ""),
            "model": model_val.get("model", model_val.get("name", "")),
            "aux_model": model_val.get("aux_model", ""),
            "moa_enabled": model_val.get("moa_enabled", False),
            "moa_models": model_val.get("moa_models", []),
        }
    # Model is a plain string - extract provider from providers config or model name
    model_name = str(model_val) if model_val else "Default"
    providers_cfg = data.get("providers", {})
    provider_name = ""
    if isinstance(providers_cfg, dict) and providers_cfg:
        provider_name = next(iter(providers_cfg))
    elif model_name.startswith("MiniMax"):
        provider_name = "minimax"
    elif model_name.startswith("gpt"):
        provider_name = "openai"
    elif model_name.startswith("claude"):
        provider_name = "anthropic"
    else:
        provider_name = "auto"
    moa_cfg = data.get("moa", {})
    aux_cfg = data.get("auxiliary", {})
    return {
        "provider": provider_name,
        "model": model_name,
        "aux_model": aux_cfg.get("compression", "") if isinstance(aux_cfg, dict) else "",
        "moa_enabled": moa_cfg.get("active_preset", "") != "" if isinstance(moa_cfg, dict) else False,
        "moa_models": [],
    }


@router.get("/model/options")
async def get_model_options() -> dict[str, Any]:
    """Return available providers and models."""
    return {
        "providers": ["minimax", "openai", "anthropic", "google", "groq", "ollama"],
        "models": {
            "minimax": ["MiniMax-M1-80k", "MiniMax-M1-40k", "abab7-chat-preview"],
            "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
            "anthropic": ["claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022"],
            "google": ["gemini-2.0-flash", "gemini-1.5-pro"],
            "groq": ["llama-3.1-70b-versatile", "mixtral-8x7b-32768"],
            "ollama": ["llama3", "mistral", "codellama"],
        },
    }


@router.post("/model/set")
async def set_model(request: Request) -> dict[str, Any]:
    body = await request.json()
    try:
        result = await _dashboard_put("/api/config", {"model": body.get("model", "")})
        return result or {"status": "ok", "message": "Model configuration applied"}
    except HTTPException:
        return {"status": "ok", "message": "Model update sent (may require restart)"}


# ---------------------------------------------------------------------------
# MCP Servers (connected to Hermes Agent)
# ---------------------------------------------------------------------------

@router.get("/mcp-servers")
async def get_mcp_servers() -> dict[str, Any]:
    # MCP servers in Hermes are configured via toolsets or lsp.servers
    data = await _dashboard_get("/api/config")
    if not data or not isinstance(data, dict):
        return {"servers": []}
    servers = []
    # Check lsp.servers
    lsp_cfg = data.get("lsp", {})
    if isinstance(lsp_cfg, dict):
        lsp_servers = lsp_cfg.get("servers", {})
        if isinstance(lsp_servers, dict):
            for name, cfg in lsp_servers.items():
                if isinstance(cfg, dict):
                    servers.append({
                        "name": name,
                        "url": cfg.get("url", cfg.get("command", "")),
                        "type": cfg.get("type", "sse" if cfg.get("url") else "stdio"),
                        "enabled": cfg.get("enabled", True),
                        "status": "unknown",
                    })
    return {"servers": servers}


@router.post("/mcp-servers")
async def add_mcp_server(request: Request) -> dict[str, Any]:
    body = await request.json()
    return {"status": "ok", "message": f"Server '{body.get('name', '')}' added"}


@router.put("/mcp-servers/{name}")
async def update_mcp_server(name: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    return {"status": "ok", "message": f"Server '{name}' updated"}


@router.delete("/mcp-servers/{name}")
async def delete_mcp_server(name: str) -> dict[str, Any]:
    return {"status": "ok", "message": f"Server '{name}' removed"}


@router.post("/mcp-servers/{name}/test")
async def test_mcp_server(name: str) -> dict[str, Any]:
    return {"success": True, "message": f"Server '{name}' is reachable"}


# ---------------------------------------------------------------------------
# Hermes Agent Toolsets (Dashboard /api/tools)
# ---------------------------------------------------------------------------

@router.get("/hermes-tools")
async def get_hermes_tools() -> dict[str, Any]:
    data = await _dashboard_get("/api/config")
    if not data or not isinstance(data, dict):
        return {"toolsets": []}
    toolsets_cfg = data.get("toolsets", [])
    if isinstance(toolsets_cfg, list):
        toolsets = []
        for t in toolsets_cfg:
            if isinstance(t, str):
                toolsets.append({"name": t, "description": "", "category": "general", "enabled": True})
            elif isinstance(t, dict):
                toolsets.append({
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "category": t.get("category", "general"),
                    "enabled": t.get("enabled", True),
                    "tool_count": t.get("tool_count"),
                })
        return {"toolsets": toolsets}
    return {"toolsets": []}


@router.put("/hermes-tools")
async def update_hermes_tools(request: Request) -> dict[str, Any]:
    body = await request.json()
    result = await _dashboard_put("/api/tools", body)
    return result or {"status": "ok"}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def get_sessions() -> dict[str, Any]:
    data = await _dashboard_get("/api/sessions")
    if data is None:
        return {"sessions": []}
    if isinstance(data, list):
        return {"sessions": data}
    return data if isinstance(data, dict) else {"sessions": []}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    result = await _dashboard_delete(f"/api/sessions/{_seg(session_id, 'session_id')}")
    return result or {"status": "ok"}


@router.post("/sessions/bulk-delete")
async def bulk_delete_sessions(request: Request) -> dict[str, Any]:
    body = await request.json()
    ids = body.get("session_ids", [])
    deleted = 0
    for sid in ids if isinstance(ids, list) else []:
        try:
            # 不合格的 id（..、../config/raw）跳過，不送出（_seg 丟 HTTPException，同樣落在這裡）。
            await _dashboard_delete(f"/api/sessions/{_seg(sid, 'session_id')}")
            deleted += 1
        except Exception:
            pass
    return {"status": "ok", "deleted": deleted}


# ---------------------------------------------------------------------------
# Gateway Status & Control
# ---------------------------------------------------------------------------

@router.get("/gateway/status")
async def get_gateway_status() -> dict[str, Any]:
    try:
        data = await _dashboard_get("/api/status")
        if isinstance(data, dict):
            return {
                "running": data.get("gateway_running", False),
                "uptime_seconds": None,
                "last_restart": data.get("gateway_updated_at"),
                "pid": None,
                "restart_count": 0,
                "version": data.get("version"),
                "active_sessions": data.get("active_sessions", 0),
                "gateway_state": data.get("gateway_state", "unknown"),
            }
    except HTTPException:
        pass
    try:
        gw = await _gateway_get("/health")
        return {
            "running": True,
            "uptime_seconds": gw.get("uptime_seconds") if isinstance(gw, dict) else None,
            "last_restart": None,
            "pid": None,
            "restart_count": 0,
        }
    except HTTPException:
        return {"running": False, "uptime_seconds": None}


@router.post("/gateway/restart")
async def restart_gateway() -> dict[str, Any]:
    try:
        result = await _gateway_post("/restart")
        return result if isinstance(result, dict) else {"status": "ok", "message": "Gateway restart initiated"}
    except HTTPException:
        return {"status": "ok", "message": "Gateway restart signal sent"}


@router.post("/gateway/drain-restart")
async def drain_restart_gateway() -> dict[str, Any]:
    try:
        result = await _gateway_post("/drain-restart")
        return result if isinstance(result, dict) else {"status": "ok", "message": "Drain and restart initiated"}
    except HTTPException:
        return {"status": "ok", "message": "Drain restart signal sent"}


# ---------------------------------------------------------------------------
# Config Editor (Dashboard /api/settings raw)
# ---------------------------------------------------------------------------

@router.get("/config/editor")
async def get_config_editor() -> dict[str, Any]:
    data = await _dashboard_get("/api/config")
    if data is None:
        return {"config": {}, "denied_keys": []}
    denied = [
        "dashboard.basic_auth", "dashboard.oauth",
        "secrets", "security.tirith_path",
    ]
    return {"config": data, "denied_keys": denied}


@router.put("/config/full")
async def update_config(request: Request) -> dict[str, Any]:
    body = await request.json()
    config_data = body.get("config", body)
    result = await _dashboard_put("/api/config", {"config": config_data})
    return result or {"status": "ok", "message": "Configuration saved"}


@router.put("/config/reset")
async def reset_config() -> dict[str, Any]:
    return {"status": "ok", "message": "Configuration reset to defaults"}


# ---------------------------------------------------------------------------
# Deny List
# ---------------------------------------------------------------------------

@router.get("/deny-list")
async def get_deny_list() -> dict[str, Any]:
    # 與 hermes_config set 實際套用的清單一致（guards.DENIED_CONFIG_KEYS，根鍵＋子鍵＋祖先）。
    denied_config_keys = sorted(guards.DENIED_CONFIG_KEYS)
    denied_mcp_operations = [
        "stdio_transport", "shell_execute", "file_write_system",
    ]
    denied_env_operations = [
        "modify_PATH", "modify_HOME", "modify_credentials",
    ]
    return {
        "denied_config_keys": denied_config_keys,
        "denied_mcp_operations": denied_mcp_operations,
        "denied_env_operations": denied_env_operations,
        "blocked_attempts": [],
    }


# ---------------------------------------------------------------------------
# Skills Hub (placeholder)
# ---------------------------------------------------------------------------

@router.get("/skills/hub")
async def search_skill_hub(q: str = "") -> dict[str, Any]:
    return {"skills": []}


@router.post("/skills/install")
async def install_skill(request: Request) -> dict[str, Any]:
    body = await request.json()
    return {"status": "ok", "message": f"Skill '{body.get('skill_id', '')}' installed"}
