"""Installed child entrypoint. Only the authenticated core proxy exposes MCP."""
import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(description="EMQX MCP, loopback Streamable HTTP")
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    os.environ['FASTMCP_SHOW_SERVER_BANNER'] = 'false'
    os.environ['FASTMCP_CHECK_FOR_UPDATES'] = 'off'
    from mcp_admin_core import request_body
    from .log_redaction import install, set_secrets
    install()
    set_secrets(os.environ.get('EMQX_MCP_API_KEY', ''), os.environ.get('EMQX_MCP_API_SECRET', ''))
    request_body.install()
    from emqx_mcp_server.server import build_server
    build_server().run(transport='streamable-http', host='127.0.0.1', port=args.port,
                       path='/mcp', show_banner=False)


if __name__ == '__main__':
    main()
