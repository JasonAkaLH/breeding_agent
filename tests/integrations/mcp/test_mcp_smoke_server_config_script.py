from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class MCPSmokeServerConfigScriptTest(unittest.TestCase):
    def test_smoke_script_outputs_non_normative_redacted_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "mcp_server_config.json"
            config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {
                                "url": "https://mcp.example.invalid/rpc",
                                "transport": "streamable_http",
                                "protocolVersion": "2025-11-25",
                                "headers": {"X-Example-Tenant": "SECRET-TENANT"},
                                "tools": [{"name": "ping", "expose": True}],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, "scripts/smoke_mcp_server_config.py", "--config", str(config), "--dry-run", "--json"],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["external_smoke_sample"]["is_normative"])
        self.assertFalse(payload["external_smoke_sample"]["server_specific_logic_allowed"])
        report = payload["servers"][0]
        self.assertEqual(report["server_name"], "demo")
        self.assertEqual(report["requested_protocol_version"], "2025-11-25")
        self.assertEqual(report["negotiated_protocol_version"], "not_executed_dry_run")
        self.assertEqual(report["adapter"], "python_legacy")
        self.assertIn("X-Example-Tenant", report["diagnostics_redaction_evidence"]["request_header_names"])
        self.assertNotIn("SECRET-TENANT", completed.stdout)
        self.assertIn("safe_no_arg_tool_call_summary", report)


if __name__ == "__main__":
    unittest.main()
