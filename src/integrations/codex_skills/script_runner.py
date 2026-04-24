from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .manifest import SkillManifest
from .script_manifest import SkillScriptEntrypoint


class SkillScriptError(RuntimeError):
    pass


class SkillScriptRunner:
    def __init__(self, *, max_stdout_bytes: int = 1024 * 1024, max_stderr_bytes: int = 64 * 1024) -> None:
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes

    async def run(
        self,
        manifest: SkillManifest,
        script: SkillScriptEntrypoint,
        input_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if script.runtime != "python":
            raise SkillScriptError(f"Unsupported skill script runtime: {script.runtime}")
        try:
            script.input_contract.validate_required(input_payload)
        except ValueError as exc:
            raise SkillScriptError(str(exc)) from exc

        script_path = self._resolve_script_path(manifest, script)
        if not script_path.exists() or not script_path.is_file():
            raise SkillScriptError(f"Skill script does not exist: {script.path}")

        stdin_bytes = json.dumps(dict(input_payload), ensure_ascii=False, default=str).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="skill-run-") as tmpdir:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                cwd=tmpdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._minimal_env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(stdin_bytes), timeout=script.timeout_seconds)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise SkillScriptError(f"Skill script timed out after {script.timeout_seconds:g}s") from exc

        if len(stdout) > self._max_stdout_bytes:
            raise SkillScriptError("Skill script stdout exceeded limit")
        stderr_text = stderr[: self._max_stderr_bytes].decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise SkillScriptError(f"Skill script failed with exit code {process.returncode}: {stderr_text}")
        try:
            decoded = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SkillScriptError("Skill script stdout must be a JSON object") from exc
        if not isinstance(decoded, dict):
            raise SkillScriptError("Skill script stdout must be a JSON object")
        try:
            script.output_contract.validate_required(decoded)
            manifest.outputs.validate_required(decoded)
        except ValueError as exc:
            raise SkillScriptError(str(exc)) from exc
        return dict(decoded)

    def _resolve_script_path(self, manifest: SkillManifest, script: SkillScriptEntrypoint) -> Path:
        raw_path = Path(script.path)
        if raw_path.is_absolute() or ".." in raw_path.parts:
            raise SkillScriptError("Skill script path must be relative and stay inside the skill package")
        root = manifest.root_dir.resolve()
        resolved = (root / raw_path).resolve()
        if not resolved.is_relative_to(root):
            raise SkillScriptError("Skill script path escapes the skill package")
        if resolved.is_symlink():
            raise SkillScriptError("Skill script symlink is not allowed")
        return resolved

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        env: dict[str, str] = {}
        python_path = Path(sys.executable).parent
        current_path = shutil.which("python")
        if current_path:
            env["PATH"] = str(python_path)
        return env
