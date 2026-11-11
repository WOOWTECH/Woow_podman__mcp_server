"""n8n child count stays within the measured PaaS memory budget."""

from mcp_admin_core.config.store import ConfigStore
from mcp_admin_core.launch_policy import set_launch_policy
from mcp_admin_core.profiles import ProfileRegistry
import mcp_admin_core.profiles as profiles

from n8n_mcp_admin.main import LAUNCH_POLICY


def test_n8n_profile_count_tracks_container_limit(tmp_path, monkeypatch):
    set_launch_policy(LAUNCH_POLICY)
    registry = ProfileRegistry(ConfigStore(tmp_path / "config.json"))
    for memory_mib, expected_extra in ((512, 0), (1024, 1), (2048, 2)):
        monkeypatch.setattr(profiles, "_memory_limit_mib", lambda value=memory_mib: value)
        assert registry.profile_limit() == expected_extra
