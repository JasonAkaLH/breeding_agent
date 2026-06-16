from __future__ import annotations

import asyncio
import json
import os
import shutil
import shlex
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Mapping

from src.storage.conversation_files import LocalConversationFileStore, mounted_input_filename

from .manifest import SkillManifest
from .script_manifest import SkillScriptEntrypoint


class SkillScriptError(RuntimeError):
    code = "skill_script_failed"


class SkillSandboxUnavailableError(SkillScriptError):
    code = "skill_runtime_sandbox_unavailable"


class SkillScriptTimeoutError(SkillScriptError):
    code = "skill_script_timeout"


class SkillScriptOutputValidationError(SkillScriptError):
    code = "skill_output_validation_failed"


SkillOutputProcessor = Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]]


def _relative_source_path(storage_key: str) -> str:
    parts = str(storage_key).split("/")
    if len(parts) >= 3:
        return "/".join(parts[1:])
    return str(storage_key)


class SkillScriptRunner:
    def __init__(
        self,
        *,
        max_stdout_bytes: int = 1024 * 1024,
        max_stderr_bytes: int = 64 * 1024,
        output_processor: SkillOutputProcessor | None = None,
        rust_sandbox_client: Any | None = None,
        rust_sandbox_mode: str = "off",
        rust_sandbox_root: str | Path | None = None,
        conversation_file_store: LocalConversationFileStore | None = None,
    ) -> None:
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._output_processor = output_processor
        self._rust_sandbox_client = rust_sandbox_client
        self._rust_sandbox_mode = rust_sandbox_mode.strip().lower()
        self._rust_sandbox_root = Path(rust_sandbox_root).resolve() if rust_sandbox_root else None
        self._conversation_file_store = conversation_file_store

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

        script_path = self._resolve_script_path(manifest, script)
        if not script_path.exists() or not script_path.is_file():
            raise SkillScriptError(f"Skill script does not exist: {script.path}")

        if self._rust_sandbox_mode == "enforce":
            output = await self._run_with_rust_sandbox(
                manifest=manifest,
                script=script,
                script_path=script_path,
                input_payload=input_payload,
                output_context=output_context,
            )
            return output

        with tempfile.TemporaryDirectory(prefix="skill-run-") as tmpdir:
            workspace_dir = Path(tmpdir)
            outputs_dir = workspace_dir / "outputs"
            outputs_dir.mkdir(parents=True, exist_ok=True)
            prepared_payload = self._prepare_resource_workspace(
                input_payload=input_payload,
                workspace_dir=workspace_dir,
                outputs_dir=outputs_dir,
            )
            try:
                script.input_contract.validate_required(prepared_payload)
            except ValueError as exc:
                raise SkillScriptError(str(exc)) from exc
            stdin_bytes = json.dumps(dict(prepared_payload), ensure_ascii=False, default=str).encode("utf-8")
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                cwd=tmpdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._minimal_env(
                    outputs_dir=outputs_dir,
                    input_dir=workspace_dir / "input",
                    resource_manifest_path=workspace_dir / "resource_manifest.json",
                    conversation_index_path=workspace_dir / "resource_index.md",
                ),
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
            return await self._process_output(
                output=output,
                outputs_dir=outputs_dir,
                manifest=manifest,
                script=script,
                output_context=output_context,
            )

    async def _run_with_rust_sandbox(
        self,
        *,
        manifest: SkillManifest,
        script: SkillScriptEntrypoint,
        script_path: Path,
        input_payload: Mapping[str, Any],
        output_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if self._rust_sandbox_client is None:
            raise SkillSandboxUnavailableError("Rust Skill Sandbox client is required in enforce mode")
        sandbox_root = self._rust_sandbox_root or manifest.root_dir.resolve()
        try:
            script_path.resolve().relative_to(sandbox_root)
        except ValueError as exc:
            raise SkillScriptError("Skill script must be under the configured Rust sandbox root") from exc

        with tempfile.TemporaryDirectory(prefix="skill-run-", dir=sandbox_root) as tmpdir:
            tmpdir_path = Path(tmpdir)
            outputs_dir = tmpdir_path / "outputs"
            outputs_dir.mkdir(parents=True, exist_ok=True)
            prepared_payload = self._prepare_resource_workspace(
                input_payload=input_payload,
                workspace_dir=tmpdir_path,
                outputs_dir=outputs_dir,
            )
            try:
                script.input_contract.validate_required(prepared_payload)
            except ValueError as exc:
                raise SkillScriptError(str(exc)) from exc
            stdin_bytes = json.dumps(dict(prepared_payload), ensure_ascii=False, default=str).encode("utf-8")
            runner_path = tmpdir_path / "run-skill-python.sh"
            script_arg = os.path.relpath(script_path, tmpdir_path)
            runner_path.write_text(
                "#!/bin/sh\n"
                "export MAF_SKILL_OUTPUT_DIR=outputs\n"
                "export MAF_SKILL_INPUT_DIR=input\n"
                "export MAF_SKILL_RESOURCE_MANIFEST=resource_manifest.json\n"
                "export MAF_SKILL_CONVERSATION_INDEX=resource_index.md\n"
                "export PYTHONUTF8=1\n"
                "export PYTHONIOENCODING=utf-8\n"
                f"exec {shlex.quote(sys.executable)} {shlex.quote(script_arg)}\n",
                encoding="utf-8",
            )
            runner_path.chmod(0o700)
            cwd_under_public_root = str(tmpdir_path.relative_to(sandbox_root))
            response = await asyncio.to_thread(
                self._rust_sandbox_client.execute_sandboxed,
                skill_name=manifest.name,
                execution_mode="python_subprocess",
                cwd_under_public_root=cwd_under_public_root,
                argv=("./run-skill-python.sh",),
                timeout_ms=max(1, int(script.timeout_seconds * 1000)),
                stdout_limit_bytes=self._max_stdout_bytes,
                stderr_limit_bytes=self._max_stderr_bytes,
                stdin_payload=stdin_bytes,
            )
            return await self._consume_process_output(
                response=response,
                manifest=manifest,
                script=script,
                outputs_dir=outputs_dir,
                output_context=output_context,
            )

    def _prepare_resource_workspace(
        self,
        *,
        input_payload: Mapping[str, Any],
        workspace_dir: Path,
        outputs_dir: Path,
    ) -> dict[str, Any]:
        input_dir = workspace_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = workspace_dir / "resource_manifest.json"
        index_path = workspace_dir / "resource_index.md"
        payload = dict(input_payload)
        uploaded_artifacts: list[dict[str, Any]] = []
        manifest_files: list[dict[str, Any]] = []
        conversation_id: str | None = None
        raw_uploaded_artifacts = payload.get("uploaded_artifacts")
        has_uploaded_artifacts = isinstance(raw_uploaded_artifacts, list | tuple) and bool(raw_uploaded_artifacts)
        for raw in raw_uploaded_artifacts or ():
            if not isinstance(raw, Mapping):
                continue
            artifact = dict(raw)
            artifact_conversation_id = str(artifact.get("conversation_id") or "").strip() or None
            if conversation_id is None and artifact_conversation_id:
                conversation_id = artifact_conversation_id
            storage_key = artifact.get("storage_key")
            if self._conversation_file_store is not None and isinstance(storage_key, str) and storage_key:
                mount_name = mounted_input_filename(
                    upload_id=str(artifact.get("upload_id") or artifact.get("file_id") or "upload"),
                    filename=str(artifact.get("original_filename") or artifact.get("filename") or "input.bin"),
                )
                mount_path = input_dir / mount_name
                self._conversation_file_store.copy_to(storage_key, mount_path)
                artifact["mount_path"] = str(mount_path)
                manifest_files.append(
                    {
                        "upload_id": artifact.get("upload_id"),
                        "filename": artifact.get("original_filename") or artifact.get("filename"),
                        "mount_path": str(mount_path),
                        "content_type": artifact.get("content_type"),
                        "file_type": artifact.get("file_type"),
                        "size_bytes": artifact.get("size_bytes") or artifact.get("size"),
                        "sha256": artifact.get("sha256"),
                        "preview": artifact.get("preview") or {},
                        "description": artifact.get("description") or {},
                        "relative_source_path": _relative_source_path(storage_key),
                    }
                )
            artifact.pop("storage_key", None)
            uploaded_artifacts.append(artifact)
        if not has_uploaded_artifacts and not manifest_files:
            self._write_resource_manifest(
                manifest_path=manifest_path,
                index_path=index_path,
                input_dir=input_dir,
                outputs_dir=outputs_dir,
                conversation_id=conversation_id,
                metadata={},
                manifest_files=manifest_files,
            )
            return payload
        payload["uploaded_artifacts"] = uploaded_artifacts
        payload["resource_manifest_path"] = str(manifest_path)
        payload["conversation_index_path"] = str(index_path)
        payload["input_dir"] = str(input_dir)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        self._write_resource_manifest(
            manifest_path=manifest_path,
            index_path=index_path,
            input_dir=input_dir,
            outputs_dir=outputs_dir,
            conversation_id=conversation_id or payload.get("conversation_id"),
            metadata=metadata,
            manifest_files=manifest_files,
        )
        return payload

    def _write_resource_manifest(
        self,
        *,
        manifest_path: Path,
        index_path: Path,
        input_dir: Path,
        outputs_dir: Path,
        conversation_id: object,
        metadata: Mapping[str, Any],
        manifest_files: list[dict[str, Any]],
    ) -> None:
        resource_manifest = {
            "version": 1,
            "conversation_id": conversation_id,
            "task_id": metadata.get("task_id"),
            "node_id": metadata.get("node_id"),
            "input_dir": str(input_dir),
            "output_dir": str(outputs_dir),
            "conversation_index_path": str(index_path),
            "files": manifest_files,
        }
        manifest_path.write_text(json.dumps(resource_manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        self._write_workspace_index(
            index_path=index_path,
            conversation_id=str(conversation_id).strip() if conversation_id else None,
        )

    def _write_workspace_index(self, *, index_path: Path, conversation_id: str | None) -> None:
        if self._conversation_file_store is not None and conversation_id:
            source = self._conversation_file_store.conversation_dir(conversation_id) / "index.md"
            if source.exists() and source.is_file():
                shutil.copy2(source, index_path)
                return
        index_path.write_text("# Conversation Files Index\n\nNo conversation files mounted.\n", encoding="utf-8")

    async def _consume_process_output(
        self,
        *,
        response: Mapping[str, Any],
        manifest: SkillManifest,
        script: SkillScriptEntrypoint,
        outputs_dir: Path,
        output_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        error = response.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or "skill_script_failed")
            message = str(error.get("message") or code)
            if code == "skill_runtime_sandbox_timeout":
                raise SkillScriptTimeoutError(message)
            raise SkillScriptError(f"{code}: {message}")
        stdout = bytes(response.get("stdout_prefix") or b"")
        stderr = bytes(response.get("stderr_prefix") or b"")
        if response.get("stdout_truncated"):
            raise SkillScriptError("Skill script stdout exceeded limit")
        stderr_text = stderr[: self._max_stderr_bytes].decode("utf-8", errors="replace")
        if int(response.get("exit_code") or 0) != 0:
            raise SkillScriptError(f"Skill script failed with exit code {int(response.get('exit_code') or 0)}: {stderr_text}")
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
        return await self._process_output(
            output=output,
            outputs_dir=outputs_dir,
            manifest=manifest,
            script=script,
            output_context=output_context,
        )

    async def _process_output(
        self,
        *,
        output: dict[str, Any],
        outputs_dir: Path,
        manifest: SkillManifest,
        script: SkillScriptEntrypoint,
        output_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if self._output_processor is None:
            return output
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
            return dict(processed)
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
    def _minimal_env(
        *,
        outputs_dir: Path,
        input_dir: Path | None = None,
        resource_manifest_path: Path | None = None,
        conversation_index_path: Path | None = None,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        python_path = Path(sys.executable).parent
        current_path = shutil.which("python")
        if current_path:
            env["PATH"] = str(python_path)
        env["MAF_SKILL_OUTPUT_DIR"] = str(outputs_dir)
        if input_dir is not None:
            env["MAF_SKILL_INPUT_DIR"] = str(input_dir)
        if resource_manifest_path is not None:
            env["MAF_SKILL_RESOURCE_MANIFEST"] = str(resource_manifest_path)
        if conversation_index_path is not None:
            env["MAF_SKILL_CONVERSATION_INDEX"] = str(conversation_index_path)
        return env
