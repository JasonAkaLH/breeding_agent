from __future__ import annotations

import unittest

from src.api.runtime import _artifact_allowlist_digests, _artifact_allowlist_entry_matches_manifest


class ArtifactTrustHelperTest(unittest.TestCase):
    def test_allowlist_matcher_requires_exact_artifact_and_nested_hash_fields(self) -> None:
        manifest = _artifact_manifest()

        self.assertTrue(_artifact_allowlist_entry_matches_manifest(dict(manifest), manifest))

        for field, value in {
            "artifact_sha256": "sha256:different",
            "cargo_features": ["different"],
            "contract_hashes": {"runtime_sidecar": "different"},
            "proto_hashes": {"runtime": "different"},
        }.items():
            entry = dict(manifest)
            entry[field] = value
            self.assertFalse(_artifact_allowlist_entry_matches_manifest(entry, manifest), field)

    def test_allowlist_digests_reject_when_manifest_is_not_exactly_allowlisted(self) -> None:
        manifest = _artifact_manifest()
        mismatched_entry = dict(manifest)
        mismatched_entry["git_commit"] = "different"

        with self.assertRaisesRegex(RuntimeError, "artifact manifest is not present in the allowlist"):
            _artifact_allowlist_digests(
                {"allowed_artifacts": [mismatched_entry]},
                required_manifest=manifest,
                artifact_label="Rust test artifact",
                raise_untrusted=_raise_untrusted,
            )

    def test_allowlist_digests_return_stable_unique_checksum_sets(self) -> None:
        manifest = _artifact_manifest()
        duplicate_entry = dict(manifest)
        unrelated_entry = dict(manifest)
        unrelated_entry.update(
            {
                "artifact_id": "other",
                "artifact_sha256": "sha256:other",
                "cargo_lock_sha256": "sha256:other-cargo-lock",
            }
        )

        checksums, cargo_lock_digests = _artifact_allowlist_digests(
            {"allowed_artifacts": [unrelated_entry, dict(manifest), duplicate_entry]},
            required_manifest=manifest,
            artifact_label="Rust test artifact",
            raise_untrusted=_raise_untrusted,
        )

        self.assertEqual(checksums, ("sha256:other", "sha256:runtime-sidecar"))
        self.assertEqual(cargo_lock_digests, ("sha256:cargo-lock", "sha256:other-cargo-lock"))


def _artifact_manifest() -> dict[str, object]:
    return {
        "component": "maf_runtime_sidecar",
        "artifact_id": "maf_runtime_sidecar",
        "artifact_kind": "sidecar_binary",
        "artifact_sha256": "sha256:runtime-sidecar",
        "cargo_lock_sha256": "sha256:cargo-lock",
        "sbom_sha256": "sha256:sbom",
        "provenance_sha256": "sha256:provenance",
        "source": "ci_pipeline",
        "git_commit": "abcdef123456",
        "toolchain": "rustc 1.95.0",
        "target_triple": "x86_64-unknown-linux-gnu",
        "build_profile": "release",
        "cargo_features": ["default"],
        "contract_hashes": {"runtime_sidecar": "runtime-schema"},
        "proto_hashes": {"runtime": "runtime-proto"},
    }


def _raise_untrusted(message: str) -> None:
    raise RuntimeError(message)


if __name__ == "__main__":
    unittest.main()
