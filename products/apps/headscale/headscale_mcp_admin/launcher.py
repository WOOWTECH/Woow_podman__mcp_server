"""Headscale MCP process; HTTP is exposed through the authenticated parent."""
import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(description="Headscale MCP")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--transport", choices=("streamable-http", "stdio"), default="streamable-http")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
    os.environ["FASTMCP_CHECK_FOR_UPDATES"] = "off"
    from mcp_admin_core import request_body
    from headscale_mcp_server.client import HeadscaleClient
    from headscale_mcp_server.server import build_server
    from .log_redaction import install, set_secrets
    install()
    key = os.environ.get("HEADSCALE_MCP_API_KEY", "")
    set_secrets(key, os.environ.get("HEADSCALE_MCP_PAAS_API_TOKEN", ""))
    request_body.install()
    client = HeadscaleClient(os.environ.get("HEADSCALE_MCP_BASE_URL", ""), key,
                             allowed_private_cidrs=tuple(x.strip() for x in os.environ.get(
                                 "HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS", "").split(",") if x.strip()))
    from .platform import NAME, POLICY, add_platform_tool
    config_path = os.environ.get("MCP_ADMIN_CONFIG", "/data/config.json")
    server = build_server(client, config_path=config_path, extra_tool_policies={NAME: POLICY})
    connection = {key: os.environ.get("HEADSCALE_MCP_" + key.upper(), "") for key in
                  ("paas_base_url", "paas_api_token", "workspace_id", "headscale_service_id")}
    add_platform_tool(server, connection, config_path)
    if args.transport == "stdio":
        server.run(transport="stdio", show_banner=False)
    else:
        server.run(transport="streamable-http", host="127.0.0.1", port=args.port,
                   path="/mcp", show_banner=False)


if __name__ == "__main__":
    main()
