from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path("scripts/rust_artifact_provenance.py")


class RustArtifactProvenanceTest(unittest.TestCase):
    def test_generate_manifest_hashes_release_inputs_without_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "maf_skill_runtime_pyo3.whl"
            cargo_lock = root / "Cargo.lock"
            sbom = root / "sbom.json"
            provenance = root / "provenance.intoto.jsonl"
            output = root / "manifest.json"
            artifact.write_bytes(b"wheel-bytes")
            cargo_lock.write_text("[[package]]\nname = \"maf_skill_runtime\"\n", encoding="utf-8")
            sbom.write_text("{\"bomFormat\":\"CycloneDX\"}\n", encoding="utf-8")
            provenance.write_text("{\"builder\":\"github-actions\"}\n", encoding="utf-8")

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "generate",
                    "--component",
                    "maf_skill_runtime",
                    "--artifact-id",
                    "maf_skill_runtime_pyo3",
                    "--artifact-kind",
                    "pyo3_wheel",
                    "--artifact-path",
                    str(artifact),
                    "--cargo-lock",
                    str(cargo_lock),
                    "--sbom",
                    str(sbom),
                    "--provenance",
                    str(provenance),
                    "--source",
                    "ci_pipeline",
                    "--git-commit",
                    "abcdef123456",
                    "--toolchain",
                    "rustc 1.95.0",
                    "--target-triple",
                    "x86_64-unknown-linux-gnu",
                    "--build-profile",
                    "release",
                    "--cargo-feature",
                    "default",
                    "--contract-hash",
                    "skill_runtime=maf_skill_runtime_schema_gates_20260515",
                    "--output",
                    str(output),
                ],
                check=True,
            )

            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "maf.rust_artifact_provenance.v1")
            self.assertEqual(manifest["component"], "maf_skill_runtime")
            self.assertEqual(manifest["artifact_id"], "maf_skill_runtime_pyo3")
            self.assertEqual(manifest["artifact_kind"], "pyo3_wheel")
            self.assertEqual(manifest["artifact_name"], artifact.name)
            self.assertTrue(manifest["artifact_sha256"].startswith("sha256:"))
            self.assertTrue(manifest["cargo_lock_sha256"].startswith("sha256:"))
            self.assertTrue(manifest["sbom_sha256"].startswith("sha256:"))
            self.assertTrue(manifest["provenance_sha256"].startswith("sha256:"))
            self.assertEqual(
                manifest["contract_hashes"]["skill_runtime"],
                "maf_skill_runtime_schema_gates_20260515",
            )
            serialized = json.dumps(manifest, sort_keys=True)
            self.assertNotIn(temp_dir, serialized)
            self.assertNotIn(str(artifact), serialized)

    def test_validate_manifest_accepts_exact_allowlisted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, allowlist = self._write_manifest_and_allowlist(Path(temp_dir))
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "validate",
                    "--manifest",
                    str(manifest),
                    "--allowlist",
                    str(allowlist),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("rust_artifact_provenance_trusted", result.stdout)

    def test_validate_manifest_fails_closed_on_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, allowlist = self._write_manifest_and_allowlist(
                Path(temp_dir),
                allowlist_overrides={"artifact_sha256": "sha256:tampered"},
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "validate",
                    "--manifest",
                    str(manifest),
                    "--allowlist",
                    str(allowlist),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("rust_artifact_provenance_untrusted", result.stderr)

    def test_validate_manifest_fails_closed_when_sbom_or_provenance_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, allowlist = self._write_manifest_and_allowlist(Path(temp_dir))
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            del payload["sbom_sha256"]
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "validate",
                    "--manifest",
                    str(manifest),
                    "--allowlist",
                    str(allowlist),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("rust_artifact_provenance_invalid", result.stderr)

    def test_self_test_exercises_allowlist_success_and_failure_paths(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "self-test"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("rust_artifact_provenance_self_test_ok", result.stdout)

    def _write_manifest_and_allowlist(
        self,
        root: Path,
        *,
        allowlist_overrides: dict[str, object] | None = None,
    ) -> tuple[Path, Path]:
        manifest_payload = {
            "schema_version": "maf.rust_artifact_provenance.v1",
            "component": "maf_skill_runtime",
            "artifact_id": "maf_skill_runtime_pyo3",
            "artifact_kind": "pyo3_wheel",
            "artifact_name": "maf_skill_runtime_pyo3-0.1.0-cp313-abi3.whl",
            "artifact_sha256": "sha256:artifact",
            "cargo_lock_sha256": "sha256:cargo-lock",
            "sbom_sha256": "sha256:sbom",
            "provenance_sha256": "sha256:provenance",
            "source": "ci_pipeline",
            "git_commit": "abcdef123456",
            "toolchain": "rustc 1.95.0",
            "target_triple": "x86_64-unknown-linux-gnu",
            "build_profile": "release",
            "cargo_features": ["default"],
            "contract_hashes": {"skill_runtime": "maf_skill_runtime_schema_gates_20260515"},
            "proto_hashes": {},
        }
        allowlist_entry = dict(manifest_payload)
        if allowlist_overrides:
            allowlist_entry.update(allowlist_overrides)
        allowlist_payload = {
            "schema_version": "maf.rust_artifact_allowlist.v1",
            "allowed_artifacts": [allowlist_entry],
        }
        manifest = root / "manifest.json"
        allowlist = root / "allowlist.json"
        manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
        allowlist.write_text(json.dumps(allowlist_payload), encoding="utf-8")
        return manifest, allowlist
