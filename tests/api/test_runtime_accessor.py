from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.api import auth
from src.api.routes import auth as auth_routes
from src.api.routes import capabilities, config, conversations, tasks, uploads
from src.api.runtime_access import runtime_from_request


class RuntimeAccessorTest(unittest.TestCase):
    def test_api_consumers_share_one_request_runtime_accessor(self) -> None:
        for module in (
            auth,
            auth_routes,
            capabilities,
            config,
            conversations,
            tasks,
            uploads,
        ):
            self.assertIs(module._runtime, runtime_from_request, module.__name__)

    def test_accessor_returns_the_exact_runtime_holder(self) -> None:
        runtime = object()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(runtime=runtime))
        )
        self.assertIs(runtime_from_request(request), runtime)


if __name__ == "__main__":
    unittest.main()
