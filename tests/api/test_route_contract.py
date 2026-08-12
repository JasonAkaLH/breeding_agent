from __future__ import annotations

import unittest
from unittest.mock import patch

from src.api.app import create_app


class APIRouteContractTest(unittest.TestCase):
    def test_non_get_api_routes_do_not_use_path_parameters(self) -> None:
        with patch.dict("os.environ", {"MAF_STATE_STORE_BACKEND": "sqlite"}, clear=False):
            app = create_app()

        approved_resource_routes = {
            "PATCH /api/v1/mcp/servers/{server_id}",
            "POST /api/v1/mcp/servers/{server_id}/test",
            "DELETE /api/v1/mcp/servers/{server_id}",
        }
        violations = [
            f"{','.join(sorted(route.methods or []))} {route.path}"
            for route in app.routes
            if route.path.startswith("/api/v1/")
            and "{" in route.path
            and any(method not in {"GET", "HEAD"} for method in (route.methods or set()))
            and f"{','.join(sorted(route.methods or []))} {route.path}" not in approved_resource_routes
        ]

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
