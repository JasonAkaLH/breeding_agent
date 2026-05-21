from __future__ import annotations

import unittest

from src.api.app import create_app


class APIRouteContractTest(unittest.TestCase):
    def test_non_get_api_routes_do_not_use_path_parameters(self) -> None:
        app = create_app()

        violations = [
            f"{','.join(sorted(route.methods or []))} {route.path}"
            for route in app.routes
            if route.path.startswith("/api/v1/")
            and "{" in route.path
            and any(method not in {"GET", "HEAD"} for method in (route.methods or set()))
        ]

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
