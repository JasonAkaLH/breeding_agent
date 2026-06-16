#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
NATIVE_DIR = REPO_ROOT / "native"
WORKSPACE_LINE_THRESHOLD = 80
SECURITY_LINE_THRESHOLD = 90
SECURITY_CRATES = (
    "maf_skill_runtime",
    "maf_mcp_runtime",
    "maf_artifact_store",
    "maf_auth_core",
    "maf_data_access",
    "maf_audit_sanitizer",
)


@dataclass(frozen=True)
class CoverageCommand:
    name: str
    command: list[str]
    cwd: Path = NATIVE_DIR
    line_coverage: int = WORKSPACE_LINE_THRESHOLD

    def to_plan(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "cwd": str(self.cwd.relative_to(REPO_ROOT)),
            "line_coverage": self.line_coverage,
        }


def build_workspace_command() -> CoverageCommand:
    return CoverageCommand(
        name="workspace_line_coverage",
        command=[
            "cargo",
            "llvm-cov",
            "--workspace",
            "--all-features",
            "--summary-only",
            "--fail-under-lines",
            str(WORKSPACE_LINE_THRESHOLD),
        ],
        line_coverage=WORKSPACE_LINE_THRESHOLD,
    )


def build_security_commands() -> list[CoverageCommand]:
    return [
        CoverageCommand(
            name=f"{crate}_line_coverage",
            command=[
                "cargo",
                "llvm-cov",
                "-p",
                crate,
                "--all-features",
                "--summary-only",
                "--fail-under-lines",
                str(SECURITY_LINE_THRESHOLD),
            ],
            line_coverage=SECURITY_LINE_THRESHOLD,
        )
        for crate in SECURITY_CRATES
    ]


def plan_payload() -> dict[str, Any]:
    workspace = build_workspace_command()
    security = build_security_commands()
    return {
        "workspace_threshold": workspace.to_plan(),
        "security_threshold": {
            "line_coverage": SECURITY_LINE_THRESHOLD,
            "crates": list(SECURITY_CRATES),
            "commands": [command.to_plan() for command in security],
        },
    }


def plan_json() -> str:
    return json.dumps(plan_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def run_thresholds() -> int:
    commands = [build_workspace_command(), *build_security_commands()]
    for command in commands:
        print(f"RUN {command.name}: {' '.join(command.command)}")
        result = subprocess.run(command.command, cwd=command.cwd, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Rust PRD01 cargo-llvm-cov line coverage thresholds.")
    parser.add_argument("--plan-json", action="store_true", help="Print the coverage threshold plan as JSON.")
    parser.add_argument("--run", action="store_true", help="Run cargo-llvm-cov threshold gates.")
    args = parser.parse_args()
    if args.plan_json:
        print(plan_json(), end="")
        return 0
    if args.run:
        return run_thresholds()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
