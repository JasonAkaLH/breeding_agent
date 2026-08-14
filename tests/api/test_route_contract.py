from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api.app import create_app
from src.api.runtime import build_api_runtime


class APIRouteContractTest(unittest.TestCase):
    def test_non_get_api_routes_do_not_use_path_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_STATE_PLATFORM_CONFIG_BRIDGE": "0",
            },
            clear=False,
        ):
            runtime = build_api_runtime(
                database_path=Path(directory) / "runtime.sqlite3",
                audit_log_path=Path(directory) / "audit.jsonl",
                master_key_bytes=b"r" * 32,
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_skill_input_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
            )
            app = create_app(runtime=runtime)
            self.addCleanup(runtime._engine.dispose)

        approved_resource_routes = {
            "PATCH /api/v1/mcp/servers/{server_id}",
            "POST /api/v1/mcp/servers/{server_id}/test",
            "DELETE /api/v1/mcp/servers/{server_id}",
            "DELETE /api/v1/mcp/servers/{server_id}/grants",
            "DELETE /api/v1/mcp/grants/{grant_id}",
            "POST /api/v1/tasks/{task_id}/mcp-calls/{call_ref}/continue",
            "POST /api/v1/tasks/{task_id}/mcp-calls/{call_ref}/cancel",
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
