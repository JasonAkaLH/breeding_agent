from __future__ import annotations

import unittest
from pathlib import Path


class FrontendNginxProxyConfigTest(unittest.TestCase):
    def test_api_doc_changelog_route_is_proxied_to_backend_not_spa_fallback(self) -> None:
        nginx_conf = Path("docker/nginx.conf").read_text(encoding="utf-8")

        self.assertIn("location = /seedpilot/api-doc", nginx_conf)
        self.assertIn("location /seedpilot/api-doc/", nginx_conf)
        self.assertIn("proxy_pass $breeding_agent_backend;", nginx_conf)
        self.assertIn("sub_filter '/api-doc/' '/seedpilot/api-doc/';", nginx_conf)
        self.assertIn("sub_filter '/openapi.json' '/seedpilot/openapi.json';", nginx_conf)

    def test_seedpilot_subpath_replaces_root_frontend_routes(self) -> None:
        nginx_conf = Path("docker/nginx.conf").read_text(encoding="utf-8")

        self.assertIn("location = / {\n        return 404;", nginx_conf)
        self.assertIn("location /seedpilot/api/ {", nginx_conf)
        self.assertIn("rewrite ^/seedpilot/api/(.*)$ /api/$1 break;", nginx_conf)
        self.assertIn("try_files $uri $uri/ /seedpilot/index.html;", nginx_conf)
        self.assertNotIn("location /api/ {", nginx_conf)
        self.assertNotIn("try_files $uri $uri/ /index.html;", nginx_conf)


if __name__ == "__main__":
    unittest.main()
