"""Real LiteLLM callbacks/guards, HTTPX MockTransport; no live gateway/provider."""
import json
from types import SimpleNamespace

import httpx
import pytest

from woow_litellm_mcp_server.errors import ToolError
from woow_litellm_mcp_server.gating import ToolGate
from woow_litellm_mcp_server.registry import ACTIVE_OPERATION_TOOLS, TOOLS_BY_NAME
from woow_litellm_mcp_server.settings import Settings
from woow_litellm_mcp_server.tools import MODULES


CASES = [
    ("litellm_health", {"model": "fake"}, "GET", "/health", {"model": "fake"}),
    ("litellm_token_counter", {"model": "fake", "prompt": "hello"},
     "POST", "/utils/token_counter", {"model": "fake", "prompt": "hello"}),
    ("litellm_spend_calculate", {"model": "fake/provider", "messages": []},
     "POST", "/spend/calculate", {"model": "fake/provider", "messages": []}),
]


class CapturedTools:
    def __init__(self, gate):
        self.tools, self.annotations = {}, {}
        for module in MODULES:
            module.register(self, gate)

    def tool(self, *, name, annotations, **kwargs):
        def capture(fn):
            self.tools[name], self.annotations[name] = fn, annotations
            return fn
        return capture


def gate_for(**overrides):
    return ToolGate(**{"readonly": False, "disabled_categories": [],
                       "disabled_tools": [], "disabled_operations": [], **overrides})


def context(client):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context={"litellm": client}))


def test_readonly_and_existing_surface():
    all_tools = CapturedTools(gate_for())
    readonly = CapturedTools(gate_for(readonly=True))
    assert len(all_tools.tools) == 40 and len(readonly.tools) == 15
    assert ACTIVE_OPERATION_TOOLS == {case[0] for case in CASES}
    assert not ACTIVE_OPERATION_TOOLS.intersection(readonly.tools)
    assert "litellm_health_readiness" in readonly.tools
    for name in ACTIVE_OPERATION_TOOLS:
        assert TOOLS_BY_NAME[name].operations == ("create",)
        hint = all_tools.annotations[name]
        assert hint.read_only_hint is False and hint.destructive_hint is False
        assert hint.idempotent_hint is False


@pytest.mark.parametrize("name,args,method,path,payload", CASES)
@pytest.mark.parametrize("policy", ["readonly", "global-create", "tool-create", "global-read", "tool-read"])
async def test_denied_stale_callback_never_reaches_backend(name, args, method, path, payload, policy):
    gate = gate_for()
    callback = CapturedTools(gate).tools[name]
    # Capture first: verifies the real handler guard, not only registration.
    if policy == "readonly":
        gate.readonly = True
    else:
        scope, operation = policy.split("-")
        gate._disabled_operations.add(operation if scope == "global" else f"{name}:{operation}")
    assert name not in CapturedTools(gate).tools
    seen = []
    def backend(request):
        seen.append(request)
        return httpx.Response(200, json={"unexpected": True})
    async with httpx.AsyncClient(base_url="http://fake.invalid", transport=httpx.MockTransport(backend)) as client:
        with pytest.raises(ToolError, match="not permitted"):
            await callback(ctx=context(client), **args)
    assert seen == []


@pytest.mark.parametrize("name,args,method,path,payload", CASES)
@pytest.mark.parametrize("mount", ["", "/proxy/v1"])
async def test_allowed_callbacks_keep_route_input_and_output(name, args, method, path, payload, mount):
    seen = []
    response = {"healthy_endpoints": ["fake"], "token_count": 7, "cost": 0.25}
    def backend(request):
        seen.append(request)
        return httpx.Response(200, json=response)
    async with httpx.AsyncClient(base_url="http://fake.invalid" + mount,
                                 transport=httpx.MockTransport(backend)) as client:
        result = await CapturedTools(gate_for()).tools[name](ctx=context(client), **args)
    assert result == response and len(seen) == 1
    request = seen[0]
    assert request.method == method and request.url.path == mount + path
    if method == "GET":
        assert dict(request.url.params) == payload
    else:
        assert json.loads(request.content) == payload and not request.url.query
    # No call_endpoint=true provider-token-count request has been introduced.


@pytest.mark.parametrize("name", sorted(ACTIVE_OPERATION_TOOLS))
def test_legacy_dict_denies_and_unrelated_create_permission(name):
    gate = gate_for(disabled_operations={name: ["read"]})
    assert name not in CapturedTools(gate).tools
    assert gate.is_tool_enabled("litellm_generate_key")
    global_read = gate_for(disabled_operations=["read"])
    assert not global_read.is_tool_enabled(name)
    assert global_read.is_tool_enabled("litellm_generate_key")


@pytest.mark.parametrize("raw,expected", [
    ("http://fake.invalid/v1/", "http://fake.invalid"),
    ("http://fake.invalid/proxy/v1/", "http://fake.invalid/proxy/v1"),
])
def test_native_settings_root_normalization(raw, expected):
    assert Settings(_env_file=None, base_url=raw).base_url == expected
