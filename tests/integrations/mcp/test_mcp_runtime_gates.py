from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.integrations.mcp.mcp_runtime_gates import (
    artifact_allowlist_entry_matches_manifest,
    load_mcp_runtime_artifact_trust,
    validate_mcp_runtime_artifact_provenance,
    validate_mcp_runtime_conformance_report,
)
from src.integrations.mcp.protocol import (
    MCP_PROTOCOL_VERSION_2024_11_05,
    MCP_PROTOCOL_VERSION_2025_03_26,
    MCP_PROTOCOL_VERSION_2025_06_18,
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCP_TRANSPORT_LEGACY_HTTP_SSE,
    MCP_TRANSPORT_STREAMABLE_HTTP,
    SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
)
from src.integrations.mcp.rust_contract import load_mcp_runtime_contract


class MCPRuntimeGateTests(unittest.TestCase):
    def test_artifact_provenance_requires_allowlisted_checksum_and_contract_hashes(self) -> None:
        contract = load_mcp_runtime_contract()
        metadata = {
            "source": "ci_pipeline",
            "artifact_kind": "mcp_runtime_sidecar_binary",
            "checksum_sha256": "sha256:mcp-sidecar",
            "cargo_lock_digest": "sha256:cargo-lock",
            "protocol_version": contract["protocol_version"],
            "schema_hash": contract["schema_hash"],
            "error_code_table_hash": contract["error_code_table_hash"],
            "proto_hash": "maf_mcp_proto_v1_20260517",
            "sbom_digest": "sha256:sbom",
            "provenance_attestation": "sha256:provenance",
        }

        result = validate_mcp_runtime_artifact_provenance(
            metadata,
            allowed_checksums={"sha256:mcp-sidecar"},
            allowed_cargo_lock_digests={"sha256:cargo-lock"},
        )

        self.assertEqual(result["artifact_kind"], "mcp_runtime_sidecar_binary")
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_artifact_untrusted"):
            validate_mcp_runtime_artifact_provenance(
                {**metadata, "schema_hash": "different"},
                allowed_checksums={"sha256:mcp-sidecar"},
                allowed_cargo_lock_digests={"sha256:cargo-lock"},
            )
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_artifact_untrusted"):
            validate_mcp_runtime_artifact_provenance(
                {**metadata, "proto_hash": "different"},
                allowed_checksums={"sha256:mcp-sidecar"},
                allowed_cargo_lock_digests={"sha256:cargo-lock"},
            )

    def test_manifest_allowlist_exact_match_loads_artifact_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = _manifest()
            manifest_path = tmp_path / "manifest.json"
            allowlist_path = tmp_path / "allowlist.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            allowlist_path.write_text(
                json.dumps({"schema_version": "maf.rust_artifact_allowlist.v1", "allowed_artifacts": [manifest]}),
                encoding="utf-8",
            )

            metadata, checksums, cargo_lock_digests = load_mcp_runtime_artifact_trust(
                manifest_path=str(manifest_path),
                allowlist_path=str(allowlist_path),
            )

            self.assertEqual(metadata["artifact_kind"], "mcp_runtime_sidecar_binary")
            self.assertEqual(checksums, ("sha256:mcp-sidecar",))
            self.assertEqual(cargo_lock_digests, ("sha256:cargo-lock",))

            mismatched = dict(manifest)
            mismatched["git_commit"] = "different"
            self.assertFalse(artifact_allowlist_entry_matches_manifest(mismatched, manifest))

    def test_manifest_and_allowlist_schema_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = _manifest()
            manifest_path = tmp_path / "manifest.json"
            allowlist_path = tmp_path / "allowlist.json"

            for changed_manifest in (
                {**manifest, "schema_version": "different"},
                {**manifest, "component": "other"},
                {**manifest, "artifact_id": "other"},
                {**manifest, "artifact_kind": "native_binary"},
            ):
                with self.subTest(changed_manifest=changed_manifest):
                    manifest_path.write_text(json.dumps(changed_manifest), encoding="utf-8")
                    allowlist_path.write_text(
                        json.dumps(
                            {
                                "schema_version": "maf.rust_artifact_allowlist.v1",
                                "allowed_artifacts": [changed_manifest],
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(RuntimeError, "mcp_runtime_artifact_untrusted"):
                        load_mcp_runtime_artifact_trust(
                            manifest_path=str(manifest_path),
                            allowlist_path=str(allowlist_path),
                        )

            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            allowlist_path.write_text(
                json.dumps({"schema_version": "different", "allowed_artifacts": [manifest]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "mcp_runtime_artifact_untrusted"):
                load_mcp_runtime_artifact_trust(
                    manifest_path=str(manifest_path),
                    allowlist_path=str(allowlist_path),
                )

    def test_conformance_report_requires_all_supported_versions_and_transport_families(self) -> None:
        report = _conformance_report()

        result = validate_mcp_runtime_conformance_report(report)

        self.assertEqual(result["supported_mcp_spec_versions"], ",".join(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1]))
        self.assertEqual(result["transport_families"], "2024-11-05=legacy_http_sse,2025+=streamable_http")

    def test_conformance_report_rejects_missing_or_extra_supported_versions(self) -> None:
        for versions in (
            SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-2],
            (*SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1], "2024-10-07"),
            (),
        ):
            with self.subTest(versions=versions):
                report = _conformance_report()
                report["supported_mcp_spec_versions"] = list(versions)
                with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
                    validate_mcp_runtime_conformance_report(report)

    def test_conformance_report_rejects_missing_version_result_or_transport_mismatch(self) -> None:
        missing_result = _conformance_report()
        del missing_result["version_results"][MCP_PROTOCOL_VERSION_2025_06_18]
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_runtime_conformance_report(missing_result)

        wrong_2024_family = _conformance_report()
        wrong_2024_family["version_results"][MCP_PROTOCOL_VERSION_2024_11_05][
            "transport_family"
        ] = MCP_TRANSPORT_STREAMABLE_HTTP
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_runtime_conformance_report(wrong_2024_family)

        wrong_2025_family = _conformance_report()
        wrong_2025_family["version_results"][MCP_PROTOCOL_VERSION_2025_03_26][
            "transport_family"
        ] = MCP_TRANSPORT_LEGACY_HTTP_SSE
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_runtime_conformance_report(wrong_2025_family)

    def test_conformance_report_requires_batch_redaction_and_safe_diagnostics(self) -> None:
        for field in ("jsonrpc_batch_rejected", "raw_id_redaction_passed", "safe_diagnostics_passed"):
            with self.subTest(field=field):
                report = _conformance_report()
                report[field] = False
                with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
                    validate_mcp_runtime_conformance_report(report)

        per_version = _conformance_report()
        per_version["version_results"][MCP_PROTOCOL_VERSION_2025_11_25]["safe_diagnostics_passed"] = False
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_runtime_conformance_report(per_version)


def _manifest() -> dict[str, object]:
    contract = load_mcp_runtime_contract()
    return {
        "schema_version": "maf.rust_artifact_provenance.v1",
        "component": "maf_mcp_runtime",
        "artifact_id": "maf_mcp_runtime_sidecar",
        "artifact_kind": "sidecar_binary",
        "artifact_name": "maf-mcp-runtime-sidecar-linux-x86_64",
        "artifact_sha256": "sha256:mcp-sidecar",
        "cargo_lock_sha256": "sha256:cargo-lock",
        "sbom_sha256": "sha256:sbom",
        "provenance_sha256": "sha256:provenance",
        "source": "ci_pipeline",
        "git_commit": "abcdef123456",
        "toolchain": "rustc 1.95.0",
        "target_triple": "x86_64-unknown-linux-gnu",
        "build_profile": "release",
        "cargo_features": ["default"],
        "contract_hashes": {
            "mcp_runtime": contract["schema_hash"],
            "mcp_runtime_errors": contract["error_code_table_hash"],
        },
        "proto_hashes": {"mcp": "maf_mcp_proto_v1_20260517"},
    }


def _conformance_report() -> dict[str, object]:
    return {
        "schema_version": "maf.mcp.client_compatibility_conformance.v1",
        "supported_mcp_spec_versions": list(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1]),
        "phase_results": {
            "phase_0": True,
            "phase_1": True,
            "phase_2": True,
            "phase_3": True,
            "phase_4": True,
            "phase_5": True,
        },
        "version_results": {
            MCP_PROTOCOL_VERSION_2024_11_05: _version_result(MCP_TRANSPORT_LEGACY_HTTP_SSE),
            MCP_PROTOCOL_VERSION_2025_03_26: _version_result(MCP_TRANSPORT_STREAMABLE_HTTP),
            MCP_PROTOCOL_VERSION_2025_06_18: _version_result(MCP_TRANSPORT_STREAMABLE_HTTP),
            MCP_PROTOCOL_VERSION_2025_11_25: _version_result(MCP_TRANSPORT_STREAMABLE_HTTP),
        },
        "jsonrpc_batch_rejected": True,
        "raw_id_redaction_passed": True,
        "safe_diagnostics_passed": True,
    }


def _version_result(transport_family: str) -> dict[str, object]:
    return {
        "initialize": True,
        "transport_family": transport_family,
        "transport": True,
        "tools_list": True,
        "tools_call": True,
        "batch_rejected": True,
        "raw_id_redaction_passed": True,
        "safe_diagnostics_passed": True,
    }


if __name__ == "__main__":
    unittest.main()
