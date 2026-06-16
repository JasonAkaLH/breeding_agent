from __future__ import annotations

import unittest
from pathlib import Path


class FrontendNginxProxyConfigTest(unittest.TestCase):
    def test_api_doc_changelog_route_is_proxied_to_backend_not_spa_fallback(self) -> None:
        nginx_conf = Path("docker/nginx.conf").read_text(encoding="utf-8")

        self.assertIn("location = /api-doc", nginx_conf)
        self.assertIn("location /api-doc/", nginx_conf)
        self.assertIn("proxy_pass $breeding_agent_backend;", nginx_conf)


if __name__ == "__main__":
    unittest.main()
