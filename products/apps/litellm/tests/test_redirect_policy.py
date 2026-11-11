"""The pooled LiteLLM client never follows an upstream Location."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from threading import Thread
import unittest

from woow_litellm_mcp_server.errors import LiteLLMApiError, litellm_request
from woow_litellm_mcp_server.lifespan import build_client
from woow_litellm_mcp_server.settings import get_settings


class RedirectPolicyTests(unittest.TestCase):
    def test_redirect_is_reported_without_second_backend_request(self):
        paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                if self.path != "/health/readiness":
                    self.send_response(500)
                else:
                    self.send_response(302)
                    self.send_header("Location", "/synthetic-target")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old = os.environ.get("LITELLM_MCP_BASE_URL")
        try:
            os.environ["LITELLM_MCP_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
            get_settings.cache_clear()

            async def exercise():
                async with build_client() as client:
                    self.assertFalse(client.follow_redirects)
                    with self.assertRaisesRegex(LiteLLMApiError, r"\(302\)"):
                        await litellm_request(client, "GET", "/health/readiness")

            asyncio.run(exercise())
            self.assertEqual(paths, ["/health/readiness"])
        finally:
            if old is None:
                os.environ.pop("LITELLM_MCP_BASE_URL", None)
            else:
                os.environ["LITELLM_MCP_BASE_URL"] = old
            get_settings.cache_clear()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
