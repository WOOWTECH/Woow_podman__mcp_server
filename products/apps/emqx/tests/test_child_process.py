"""Launch the real loopback MCP child twice; synthetic HTTP backend only."""
import importlib.util
from pathlib import Path


async def test_executable_child_policy_and_restart():
    script = Path(__file__).parents[1] / 'scripts' / 'ci_smoke_child.py'
    spec = importlib.util.spec_from_file_location('emqx_child_smoke', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = await module.smoke()
    assert receipt['status'] == 'PASS'
    assert receipt['child_generations'] == 2
