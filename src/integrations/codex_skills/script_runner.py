from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Mapping

from .manifest import SkillManifest
from .script_manifest import SkillScriptEntrypoint


class SkillScriptError(RuntimeError):
    code = "skill_script_failed"


class SkillScriptTimeoutError(SkillScriptError):
    code = "skill_script_timeout"


class SkillScriptOutputValidationError(SkillScriptError):
    code = "skill_output_validation_failed"


SkillOutputProcessor = Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]]


class SkillScriptRunner:
    def __init__(
        self,
        *,
        max_stdout_bytes: int = 1024 * 1024,
        max_stderr_bytes: int = 64 * 1024,
        output_processor: SkillOutputProcessor | None = None,
    ) -> None:
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._output_processor = output_processor

    async def run(
        self,
        manifest: SkillManifest,
        script: SkillScriptEntrypoint,
        input_payload: Mapping[str, Any],
        *,
        output_context: Mapping[str, Any] | None = None,
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
            outputs_dir = Path(tmpdir) / "outputs"
            outputs_dir.mkdir(parents=True, exist_ok=True)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                cwd=tmpdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._minimal_env(outputs_dir=outputs_dir),
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(stdin_bytes), timeout=script.timeout_seconds)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise SkillScriptTimeoutError(f"Skill script timed out after {script.timeout_seconds:g}s") from exc

            if len(stdout) > self._max_stdout_bytes:
                raise SkillScriptError("Skill script stdout exceeded limit")
            stderr_text = stderr[: self._max_stderr_bytes].decode("utf-8", errors="replace")
            if process.returncode != 0:
                raise SkillScriptError(f"Skill script failed with exit code {process.returncode}: {stderr_text}")
            try:
                decoded = json.loads(stdout.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise SkillScriptOutputValidationError("Skill script stdout must be a JSON object") from exc
            if not isinstance(decoded, dict):
                raise SkillScriptOutputValidationError("Skill script stdout must be a JSON object")
            try:
                script.output_contract.validate_required(decoded)
                manifest.outputs.validate_required(decoded)
            except ValueError as exc:
                raise SkillScriptOutputValidationError(str(exc)) from exc
            output = dict(decoded)
            if self._output_processor is not None:
                try:
                    processed = self._output_processor(
                        output=output,
                        outputs_dir=outputs_dir,
                        manifest=manifest,
                        script=script,
                        context=dict(output_context or {}),
                    )
                    if hasattr(processed, "__await__"):
                        processed = await processed  # type: ignore[assignment]
                    if not isinstance(processed, dict):
                        raise SkillScriptError("Skill output processor must return a JSON object")
                    output = dict(processed)
                except SkillScriptError:
                    raise
                except Exception as exc:
                    output.pop("output_files", None)
                    output["output_file_diagnostics"] = [
                        {
                            "path": "",
                            "reason": "output_processing_failed",
                            "message": exc.__class__.__name__,
                        }
                    ]
            return output

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
    def _minimal_env(*, outputs_dir: Path) -> dict[str, str]:
        env: dict[str, str] = {}
        python_path = Path(sys.executable).parent
        current_path = shutil.which("python")
        if current_path:
            env["PATH"] = str(python_path)
        env["MAF_SKILL_OUTPUT_DIR"] = str(outputs_dir)
        return env
