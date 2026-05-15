#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
NATIVE_DIR = REPO_ROOT / "native"
PYTHON_ENV = "multi_agent"


@dataclass(frozen=True)
class Gate:
    name: str
    command: list[str]
    cwd: Path = NATIVE_DIR
    env: dict[str, str] = field(default_factory=dict)
    required_tools: tuple[str, ...] = ()
    description: str = ""

    def to_plan(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "cwd": str(self.cwd.relative_to(REPO_ROOT)),
            "env": dict(self.env),
            "required_tools": list(self.required_tools),
            "description": self.description,
        }


def build_gates() -> list[Gate]:
    return [
        Gate(
            name="cargo_fmt",
            command=["cargo", "fmt", "--check"],
            required_tools=("cargo",),
            description="PRD01 required formatting gate.",
        ),
        Gate(
            name="cargo_clippy",
            command=[
                "cargo",
                "clippy",
                "--workspace",
                "--all-targets",
                "--all-features",
                "--",
                "-D",
                "warnings",
            ],
            required_tools=("cargo",),
            description="PRD01 required lint gate; fails on warnings.",
        ),
        Gate(
            name="cargo_test",
            command=["cargo", "test", "--workspace", "--all-features"],
            required_tools=("cargo",),
            description="PRD01 canonical cargo test gate.",
        ),
        Gate(
            name="cargo_nextest",
            command=["cargo", "nextest", "run", "--workspace", "--all-features"],
            required_tools=("cargo", "cargo-nextest"),
            description="PRD01 nextest gate; does not replace cargo test.",
        ),
        Gate(
            name="cargo_audit",
            command=["cargo", "audit"],
            required_tools=("cargo", "cargo-audit"),
            description="PRD01 advisory / vulnerability gate.",
        ),
        Gate(
            name="cargo_deny",
            command=["cargo", "deny", "check"],
            required_tools=("cargo", "cargo-deny"),
            description="PRD01 license / advisory / duplicate-source policy gate.",
        ),
        Gate(
            name="cargo_llvm_cov",
            command=["cargo", "llvm-cov", "--workspace", "--all-features", "--summary-only"],
            required_tools=("cargo", "cargo-llvm-cov"),
            description="PRD01 coverage summary gate; release CI must enforce thresholds.",
        ),
        Gate(
            name="rust_coverage_thresholds",
            command=[sys.executable, "scripts/run_rust_coverage_thresholds.py", "--run"],
            cwd=REPO_ROOT,
            required_tools=("cargo", "cargo-llvm-cov"),
            description="PRD01 line coverage gate: workspace >=80%, security-sensitive crates >=90%.",
        ),
        Gate(
            name="fuzz_cargo_check",
            command=["cargo", "check", "--manifest-path", "native/fuzz/Cargo.toml", "--bins"],
            cwd=REPO_ROOT,
            required_tools=("cargo",),
            description="Compile all PRD01 fuzz harnesses without executing libFuzzer.",
        ),
        Gate(
            name="cargo_fuzz_smoke",
            command=["cargo", "+nightly", "fuzz", "run", "skill_runtime_policy", "--", "-max_total_time=30"],
            cwd=REPO_ROOT / "native" / "fuzz",
            required_tools=("cargo", "cargo-fuzz"),
            description="Bounded PRD01 fuzz smoke for the Skill Runtime policy boundary.",
        ),
        Gate(
            name="rust_artifact_provenance_self_check",
            command=[sys.executable, "scripts/rust_artifact_provenance.py", "self-test"],
            cwd=REPO_ROOT,
            description="PRD01 release artifact checksum / SBOM / provenance / allowlist fail-closed smoke.",
        ),
        Gate(
            name="skill_runtime_pyo3_wheel_smoke",
            command=[
                "conda",
                "run",
                "-n",
                PYTHON_ENV,
                "python",
                "-m",
                "maturin",
                "build",
                "--release",
                "--locked",
                "--manifest-path",
                "native/crates/maf_skill_runtime_pyo3/Cargo.toml",
                "--interpreter",
                sys.executable,
                "--compatibility",
                "manylinux_2_35",
                "--auditwheel",
                "check",
                "--out",
                "native/target/wheels",
            ],
            cwd=REPO_ROOT,
            env={"CARGO_BUILD_JOBS": "1"},
            required_tools=("conda", "cargo"),
            description=(
                "PRD01/04 PyO3 wheel build smoke; Ubuntu 22.04 x86_64 CI uses "
                "manylinux_2_35, production still requires provenance allowlist evidence."
            ),
        ),
        Gate(
            name="fuzz_target_manifest",
            command=[sys.executable, "scripts/run_rust_quality_gates.py", "--check-fuzz-targets"],
            cwd=REPO_ROOT,
            description="PRD01 fuzz readiness manifest check for untrusted-input boundaries.",
        ),
    ]


def plan_json() -> str:
    payload = {
        "workspace": "native",
        "python_env": PYTHON_ENV,
        "gates": [gate.to_plan() for gate in build_gates()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def check_fuzz_targets() -> int:
    fuzz_dir = NATIVE_DIR / "fuzz" / "fuzz_targets"
    required = {
        "skill_runtime_policy.rs",
        "mcp_runtime_protocol.rs",
        "artifact_path.rs",
        "audit_sanitizer.rs",
        "data_access_readonly.rs",
    }
    existing = {path.name for path in fuzz_dir.glob("*.rs")} if fuzz_dir.exists() else set()
    missing = sorted(required - existing)
    if missing:
        print(
            "Missing required PRD01 fuzz targets: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    return 0


def tool_is_available(tool: str) -> bool:
    return shutil.which(tool) is not None


def run_gates(selected: set[str] | None, *, skip_unavailable: bool) -> int:
    gates = [gate for gate in build_gates() if selected is None or gate.name in selected]
    if selected is not None:
        unknown = selected - {gate.name for gate in build_gates()}
        if unknown:
            print(f"Unknown Rust quality gate(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
    for gate in gates:
        missing_tools = [tool for tool in gate.required_tools if not tool_is_available(tool)]
        if missing_tools:
            message = f"{gate.name}: missing required tool(s): {', '.join(missing_tools)}"
            if skip_unavailable:
                print(f"SKIP {message}")
                continue
            print(message, file=sys.stderr)
            return 1
        env = {**gate.env} if gate.env else None
        print(f"RUN {gate.name}: {' '.join(gate.command)}")
        result = subprocess.run(
            gate.command,
            cwd=gate.cwd,
            env=None if env is None else {**os.environ, **env},
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or print the Rust PRD01 quality gate matrix.")
    parser.add_argument("--plan-json", action="store_true", help="Print the gate matrix as JSON and exit.")
    parser.add_argument("--check-fuzz-targets", action="store_true", help="Check required fuzz target manifests.")
    parser.add_argument("--run", action="store_true", help="Run gates instead of printing help.")
    parser.add_argument("--only", action="append", default=[], help="Run only a named gate; can be repeated.")
    parser.add_argument("--skip-unavailable", action="store_true", help="Skip gates whose external tool is not installed.")
    args = parser.parse_args()

    if args.plan_json:
        print(plan_json(), end="")
        return 0
    if args.check_fuzz_targets:
        return check_fuzz_targets()
    if args.run:
        selected = set(args.only) if args.only else None
        return run_gates(selected, skip_unavailable=args.skip_unavailable)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
