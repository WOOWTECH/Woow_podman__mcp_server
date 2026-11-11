"""讓 ``python -m hermes_mcp_server`` 等同 console script ``hermes-mcp-server``。"""
from __future__ import annotations

from hermes_mcp_server.launcher import main

if __name__ == "__main__":
    main()
