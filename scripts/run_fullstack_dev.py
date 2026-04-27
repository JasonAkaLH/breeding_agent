#!/usr/bin/env python3
"""Start the development backend and frontend for manual validation.

Default mode uses the repository's real FastAPI runtime. Pass --fake-backend
when you need a deterministic local backend that does not require real LLM or
MySQL provider configuration.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
RUNTIME_DIR = ROOT / "runtime"


def _wait_for_tcp(host: str, port: int, *, timeout: float, label: str, process: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"{label} exited before it was ready with code {process.returncode}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {label} at {host}:{port}")


def _run_checked(command: Sequence[str], *, cwd: Path, env: dict[str, str]) -> None:
    print(f"[fullstack] Running: {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _fake_backend_code(host: str, port: int, database_path: Path, audit_log_path: Path) -> str:
    return f"""
from pathlib import Path
import uvicorn

from src.api.app import create_app
from src.api.runtime import build_api_runtime
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult


def main_agent_stream(_prompt):
    return ["开发验证模式：主代理已收到你的问题，", "这是通过 SSE 流式返回的模拟回答。"]


def sql_runner(_sql):
    return ReadonlyQueryResult(
        columns=("variety_name", "gene_marker", "summary"),
        rows=(
            {{"variety_name": "龙粳33", "gene_marker": "DEV-G1", "summary": "开发验证数据：基因型预览"}},
            {{"variety_name": "龙粳33", "gene_marker": "DEV-G2", "summary": "开发验证数据：籼粳成分预览"}},
        ),
        row_count=2,
    )


def summarize(payload):
    return f"开发验证查询完成，共返回 {{payload.get('row_count', 0)}} 行结果。"


runtime = build_api_runtime(
    database_path=Path({str(database_path)!r}),
    audit_log_path=Path({str(audit_log_path)!r}),
    mysql_adapter=MySQLReadonlyAdapter(runner=sql_runner),
    summarizer=summarize,
    main_agent_stream_generator=main_agent_stream,
    skill_roots=[],
)
app = create_app(runtime=runtime)
uvicorn.run(app, host={host!r}, port={port!r}, log_level="info")
"""


def _python_command(args: argparse.Namespace) -> list[str]:
    if importlib.util.find_spec("sqlalchemy") is not None and importlib.util.find_spec("fastapi") is not None:
        return [sys.executable]
    conda = shutil.which("conda")
    if conda:
        return [conda, "run", "--no-capture-output", "-n", args.conda_env, "python"]
    return [sys.executable]


def _start_backend(args: argparse.Namespace, env: dict[str, str]) -> subprocess.Popen:
    python_cmd = _python_command(args)
    if not args.fake_backend:
        mode_label = "real repository runtime"
        command = [
            *python_cmd,
            "-m",
            "uvicorn",
            "src.api.app:create_app",
            "--factory",
            "--host",
            args.backend_host,
            "--port",
            str(args.backend_port),
            "--reload",
        ]
    else:
        mode_label = "deterministic fake providers"
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        database_path = Path(args.database_path) if args.database_path else RUNTIME_DIR / "frontend-dev.sqlite3"
        audit_log_path = Path(args.audit_log_path) if args.audit_log_path else RUNTIME_DIR / "frontend-dev-audit.jsonl"
        command = [
            *python_cmd,
            "-c",
            _fake_backend_code(args.backend_host, args.backend_port, database_path, audit_log_path),
        ]
    print(
        f"[fullstack] Starting backend on http://{args.backend_host}:{args.backend_port} ({mode_label})",
        flush=True,
    )
    return subprocess.Popen(command, cwd=ROOT, env=env)


def _start_frontend(args: argparse.Namespace, env: dict[str, str]) -> subprocess.Popen:
    frontend_env = dict(env)
    frontend_env["VITE_API_PROXY_TARGET"] = f"http://{args.backend_host}:{args.backend_port}"
    frontend_env["VITE_DEV_PORT"] = str(args.frontend_port)
    command = ["npm", "run", "dev", "--", "--port", str(args.frontend_port)]
    print(f"[fullstack] Starting frontend on http://127.0.0.1:{args.frontend_port}", flush=True)
    return subprocess.Popen(command, cwd=FRONTEND_DIR, env=frontend_env)


def _terminate(processes: Sequence[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 8
    for process in processes:
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Start full-stack dev servers for manual validation.")
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--database-path", default=None, help="SQLite path for --fake-backend mode.")
    parser.add_argument("--audit-log-path", default=None, help="Audit JSONL path for --fake-backend mode.")
    backend_mode = parser.add_mutually_exclusive_group()
    backend_mode.add_argument("--real-backend", action="store_true", help="Use src.api.app:create_app default runtime. This is the default and is kept for compatibility.")
    backend_mode.add_argument("--fake-backend", action="store_true", help="Use deterministic fake LLM/MySQL providers for local UI-only validation.")
    parser.add_argument("--conda-env", default="multi_agent", help="Conda env used when the current Python lacks backend dependencies.")
    parser.add_argument("--no-install", action="store_true", help="Do not run npm install when frontend/node_modules is missing.")
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    args = parser.parse_args()

    if not FRONTEND_DIR.exists():
        raise FileNotFoundError(f"Frontend directory not found: {FRONTEND_DIR}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    if not args.no_install and not (FRONTEND_DIR / "node_modules").exists():
        _run_checked(["npm", "install"], cwd=FRONTEND_DIR, env=env)

    processes: list[subprocess.Popen] = []
    try:
        backend = _start_backend(args, env)
        processes.append(backend)
        _wait_for_tcp(args.backend_host, args.backend_port, timeout=args.startup_timeout, label="backend", process=backend)

        frontend = _start_frontend(args, env)
        processes.append(frontend)
        _wait_for_tcp("127.0.0.1", args.frontend_port, timeout=args.startup_timeout, label="frontend", process=frontend)

        print("\n[fullstack] Ready for manual validation", flush=True)
        print(f"  Frontend: http://127.0.0.1:{args.frontend_port}", flush=True)
        print(f"  Backend:  http://{args.backend_host}:{args.backend_port}", flush=True)
        print("  Press Ctrl+C to stop both servers.\n", flush=True)

        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        return next((process.returncode or 0 for process in processes if process.poll() is not None), 0)
    except KeyboardInterrupt:
        print("\n[fullstack] Stopping servers...", flush=True)
        return 0
    finally:
        _terminate(processes)


if __name__ == "__main__":
    raise SystemExit(main())
