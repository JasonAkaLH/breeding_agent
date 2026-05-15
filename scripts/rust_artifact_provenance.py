#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "maf.rust_artifact_provenance.v1"
ALLOWLIST_SCHEMA_VERSION = "maf.rust_artifact_allowlist.v1"
TRUSTED_SOURCES = {
    "ci_pipeline",
    "deployment_pipeline",
    "skill_ci",
    "runtime_allowlist",
}
ARTIFACT_KINDS = {
    "pyo3_wheel",
    "sidecar_binary",
    "sidecar_image",
    "native_binary",
    "skill_owned_rust_artifact",
}
REQUIRED_MANIFEST_FIELDS = (
    "schema_version",
    "component",
    "artifact_id",
    "artifact_kind",
    "artifact_name",
    "artifact_sha256",
    "cargo_lock_sha256",
    "sbom_sha256",
    "provenance_sha256",
    "source",
    "git_commit",
    "toolchain",
    "target_triple",
    "build_profile",
    "cargo_features",
    "contract_hashes",
    "proto_hashes",
)
ALLOWLIST_MATCH_FIELDS = (
    "component",
    "artifact_id",
    "artifact_kind",
    "artifact_sha256",
    "cargo_lock_sha256",
    "sbom_sha256",
    "provenance_sha256",
    "source",
    "git_commit",
    "toolchain",
    "target_triple",
    "build_profile",
    "cargo_features",
    "contract_hashes",
    "proto_hashes",
)
IDENTITY_FIELDS = (
    "component",
    "artifact_id",
    "artifact_kind",
    "target_triple",
    "build_profile",
)
SENSITIVE_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "dsn",
    "provider_key",
    "base_url",
    "private_key",
)


class ProvenanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def sha256_uri(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def parse_key_value(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ProvenanceError(
                "rust_artifact_provenance_invalid",
                f"expected KEY=VALUE pair, got {value!r}",
            )
        key, item = value.split("=", 1)
        key = key.strip()
        item = item.strip()
        if not key or not item:
            raise ProvenanceError(
                "rust_artifact_provenance_invalid",
                f"empty KEY or VALUE in pair {value!r}",
            )
        parsed[key] = item
    return parsed


def ensure_existing_file(path: Path, field_name: str) -> Path:
    if not path.is_file():
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            f"{field_name} does not exist or is not a file",
        )
    return path


def generate_manifest(args: argparse.Namespace) -> dict[str, Any]:
    artifact = ensure_existing_file(args.artifact_path, "artifact_path")
    cargo_lock = ensure_existing_file(args.cargo_lock, "cargo_lock")
    sbom = ensure_existing_file(args.sbom, "sbom")
    provenance = ensure_existing_file(args.provenance, "provenance")
    if args.source not in TRUSTED_SOURCES:
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            f"source {args.source!r} is not an approved Rust artifact source",
        )
    if args.artifact_kind not in ARTIFACT_KINDS:
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            f"artifact_kind {args.artifact_kind!r} is not supported",
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "component": args.component,
        "artifact_id": args.artifact_id,
        "artifact_kind": args.artifact_kind,
        "artifact_name": artifact.name,
        "artifact_sha256": sha256_uri(artifact),
        "cargo_lock_sha256": sha256_uri(cargo_lock),
        "sbom_sha256": sha256_uri(sbom),
        "provenance_sha256": sha256_uri(provenance),
        "source": args.source,
        "git_commit": args.git_commit,
        "toolchain": args.toolchain,
        "target_triple": args.target_triple,
        "build_profile": args.build_profile,
        "cargo_features": sorted(set(args.cargo_feature or [])),
        "contract_hashes": parse_key_value(args.contract_hash or []),
        "proto_hashes": parse_key_value(args.proto_hash or []),
    }
    validate_manifest_shape(manifest)
    return manifest


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            f"{path.name} is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            f"{path.name} must contain a JSON object",
        )
    return payload


def validate_manifest_shape(manifest: dict[str, Any]) -> None:
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing:
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            "manifest missing required field(s): " + ", ".join(missing),
        )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            "manifest schema_version is not supported",
        )
    if manifest["artifact_kind"] not in ARTIFACT_KINDS:
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            "manifest artifact_kind is not supported",
        )
    if manifest["source"] not in TRUSTED_SOURCES:
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            "manifest source is not trusted",
        )
    for field in ("artifact_sha256", "cargo_lock_sha256", "sbom_sha256", "provenance_sha256"):
        value = manifest[field]
        if not isinstance(value, str) or not value.startswith("sha256:") or len(value) <= len("sha256:"):
            raise ProvenanceError(
                "rust_artifact_provenance_invalid",
                f"manifest {field} must be a sha256: URI",
            )
    if not isinstance(manifest["cargo_features"], list) or not all(
        isinstance(item, str) for item in manifest["cargo_features"]
    ):
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            "manifest cargo_features must be a list of strings",
        )
    for field in ("contract_hashes", "proto_hashes"):
        value = manifest[field]
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise ProvenanceError(
                "rust_artifact_provenance_invalid",
                f"manifest {field} must be an object of string hashes",
            )
    reject_sensitive_metadata(manifest)


def reject_sensitive_metadata(payload: Any, *, path: str = "$") -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
                raise ProvenanceError(
                    "rust_artifact_provenance_invalid",
                    f"sensitive metadata key is forbidden at {path}.{key}",
                )
            reject_sensitive_metadata(value, path=f"{path}.{key}")
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            reject_sensitive_metadata(item, path=f"{path}[{index}]")


def validate_allowlist_shape(allowlist: dict[str, Any]) -> list[dict[str, Any]]:
    if allowlist.get("schema_version") != ALLOWLIST_SCHEMA_VERSION:
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            "allowlist schema_version is not supported",
        )
    entries = allowlist.get("allowed_artifacts")
    if not isinstance(entries, list):
        raise ProvenanceError(
            "rust_artifact_provenance_invalid",
            "allowlist allowed_artifacts must be a list",
        )
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProvenanceError(
                "rust_artifact_provenance_invalid",
                "allowlist entries must be JSON objects",
            )
        validate_manifest_shape({**entry, "schema_version": SCHEMA_VERSION})
        normalized.append(entry)
    return normalized


def validate_against_allowlist(
    *,
    manifest: dict[str, Any],
    allowlist: dict[str, Any],
    artifact_path: Path | None = None,
) -> None:
    validate_manifest_shape(manifest)
    if artifact_path is not None:
        ensure_existing_file(artifact_path, "artifact_path")
        actual = sha256_uri(artifact_path)
        if actual != manifest["artifact_sha256"]:
            raise ProvenanceError(
                "rust_artifact_provenance_untrusted",
                "artifact bytes do not match manifest checksum",
            )
    entries = validate_allowlist_shape(allowlist)
    identity_matches = [
        entry for entry in entries if all(entry.get(field) == manifest.get(field) for field in IDENTITY_FIELDS)
    ]
    for entry in identity_matches:
        if all(entry.get(field) == manifest.get(field) for field in ALLOWLIST_MATCH_FIELDS):
            return
    if identity_matches:
        raise ProvenanceError(
            "rust_artifact_provenance_untrusted",
            "artifact identity is allowlisted but checksum/provenance/SBOM/contract metadata differs",
        )
    raise ProvenanceError(
        "rust_artifact_provenance_untrusted",
        "artifact identity is not present in the runtime allowlist",
    )


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        artifact = root / "artifact.bin"
        cargo_lock = root / "Cargo.lock"
        sbom = root / "sbom.json"
        provenance = root / "provenance.json"
        artifact.write_bytes(b"artifact")
        cargo_lock.write_text("[[package]]\nname = \"maf\"\n", encoding="utf-8")
        sbom.write_text("{\"bomFormat\":\"CycloneDX\"}\n", encoding="utf-8")
        provenance.write_text("{\"builder\":\"ci\"}\n", encoding="utf-8")
        args = argparse.Namespace(
            component="maf_skill_runtime",
            artifact_id="maf_skill_runtime_pyo3",
            artifact_kind="pyo3_wheel",
            artifact_path=artifact,
            cargo_lock=cargo_lock,
            sbom=sbom,
            provenance=provenance,
            source="ci_pipeline",
            git_commit="abcdef123456",
            toolchain="rustc 1.95.0",
            target_triple="x86_64-unknown-linux-gnu",
            build_profile="release",
            cargo_feature=["default"],
            contract_hash=["skill_runtime=maf_skill_runtime_schema_gates_20260515"],
            proto_hash=[],
        )
        manifest = generate_manifest(args)
        allowlist = {
            "schema_version": ALLOWLIST_SCHEMA_VERSION,
            "allowed_artifacts": [dict(manifest)],
        }
        validate_against_allowlist(manifest=manifest, allowlist=allowlist)
        tampered = dict(manifest)
        tampered["artifact_sha256"] = "sha256:tampered"
        try:
            validate_against_allowlist(manifest=tampered, allowlist=allowlist)
        except ProvenanceError as exc:
            if exc.code != "rust_artifact_provenance_untrusted":
                raise
        else:
            raise ProvenanceError(
                "rust_artifact_provenance_invalid",
                "self-test expected tampered checksum to fail closed",
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and validate Rust artifact provenance manifests.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate a Rust artifact provenance manifest.")
    generate.add_argument("--component", required=True)
    generate.add_argument("--artifact-id", required=True)
    generate.add_argument("--artifact-kind", required=True, choices=sorted(ARTIFACT_KINDS))
    generate.add_argument("--artifact-path", required=True, type=Path)
    generate.add_argument("--cargo-lock", required=True, type=Path)
    generate.add_argument("--sbom", required=True, type=Path)
    generate.add_argument("--provenance", required=True, type=Path)
    generate.add_argument("--source", required=True, choices=sorted(TRUSTED_SOURCES))
    generate.add_argument("--git-commit", required=True)
    generate.add_argument("--toolchain", required=True)
    generate.add_argument("--target-triple", required=True)
    generate.add_argument("--build-profile", required=True)
    generate.add_argument("--cargo-feature", action="append", default=[])
    generate.add_argument("--contract-hash", action="append", default=[])
    generate.add_argument("--proto-hash", action="append", default=[])
    generate.add_argument("--output", required=True, type=Path)

    validate = subparsers.add_parser("validate", help="Validate a Rust artifact manifest against an allowlist.")
    validate.add_argument("--manifest", required=True, type=Path)
    validate.add_argument("--allowlist", required=True, type=Path)
    validate.add_argument("--artifact-path", type=Path)

    subparsers.add_parser("self-test", help="Run local success/failure smoke checks for the provenance gate.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "generate":
            manifest = generate_manifest(args)
            write_json(args.output, manifest)
            print("rust_artifact_provenance_manifest_generated")
            return 0
        if args.command == "validate":
            manifest = load_json(args.manifest)
            allowlist = load_json(args.allowlist)
            validate_against_allowlist(
                manifest=manifest,
                allowlist=allowlist,
                artifact_path=args.artifact_path,
            )
            print("rust_artifact_provenance_trusted")
            return 0
        if args.command == "self-test":
            run_self_test()
            print("rust_artifact_provenance_self_test_ok")
            return 0
    except ProvenanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
