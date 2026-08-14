from __future__ import annotations

import base64
import json
import os
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.integrations.master_key import (
    MasterKeyDeriver,
    MasterKeyDomain,
    MasterKeyError,
    _DerivedDomainKey,
)


_ROOT_KEY = bytes(range(32))
_DOMAIN_VECTORS = {
    MasterKeyDomain.MCP_CREDENTIAL: "2b2c51735555cdc190df23c718648e83e0ca3fba057f430b1fd2d33c064a408d",
    MasterKeyDomain.MCP_RECOVERY: "318ea8cd29b5c71b851bf81804bc8f4d5991415980a6ceae9363f3654735fd4e",
    MasterKeyDomain.AUTH_TOKEN: "83de2cf9e995abc6ec3c15b5125820432dce763304e434a46efcefbeade295ac",
    MasterKeyDomain.MCP_AUDIT_REFERENCE: "ce2689f54332602691edd872e907090a9f19a49d12caed7e8f8c80e981898587",
    MasterKeyDomain.KEY_VALIDATION: "396d140d55506c67f697515d9654d9e3f7b2ba4dabb561ca54d006a85a78262f",
}


class MasterKeyDeriverTests(unittest.TestCase):
    def test_domain_labels_are_exact_and_closed(self) -> None:
        self.assertEqual(
            {domain.name: domain.value for domain in MasterKeyDomain},
            {
                "MCP_CREDENTIAL": b"maf/mcp-credential-aes-gcm/v1",
                "MCP_RECOVERY": b"maf/mcp-recovery-aes-gcm/v1",
                "AUTH_TOKEN": b"maf/auth-token-hmac-sha256/v1",
                "MCP_AUDIT_REFERENCE": b"maf/mcp-audit-reference-hmac/v1",
                "KEY_VALIDATION": b"maf/key-validation-aes-gcm/v1",
            },
        )

    def test_hkdf_vectors_are_stable_and_domain_separated(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(_ROOT_KEY)
        derived = {
            domain: deriver.derive(domain)._consume_for(domain).hex()
            for domain in MasterKeyDomain
        }
        self.assertEqual(derived, _DOMAIN_VECTORS)
        self.assertEqual(len(set(derived.values())), len(MasterKeyDomain))

    def test_different_roots_produce_different_keys_for_every_domain(self) -> None:
        first = MasterKeyDeriver.from_bytes(b"a" * 32)
        second = MasterKeyDeriver.from_bytes(b"b" * 32)
        for domain in MasterKeyDomain:
            self.assertNotEqual(
                first.derive(domain)._consume_for(domain),
                second.derive(domain)._consume_for(domain),
            )

    def test_unknown_domain_and_invalid_root_length_are_rejected(self) -> None:
        with self.assertRaisesRegex(MasterKeyError, "maf_key_domain_invalid"):
            MasterKeyDeriver.from_bytes(_ROOT_KEY).derive("MCP_CREDENTIAL")  # type: ignore[arg-type]
        for invalid in (b"", b"x" * 31, b"x" * 33, bytearray(b"x" * 32)):
            with self.subTest(invalid_type=type(invalid).__name__, length=len(invalid)):
                with self.assertRaisesRegex(
                    MasterKeyError, "maf_master_key_invalid_length"
                ):
                    MasterKeyDeriver.from_bytes(invalid)  # type: ignore[arg-type]

    def test_derived_key_is_domain_bound_redacted_and_not_serializable(self) -> None:
        derived = MasterKeyDeriver.from_bytes(_ROOT_KEY).derive(
            MasterKeyDomain.AUTH_TOKEN
        )
        self.assertEqual(repr(derived), "<_DerivedDomainKey redacted>")
        self.assertNotIn(_ROOT_KEY.hex(), repr(derived))
        for attribute in ("key", "key_bytes", "domain", "__dict__"):
            self.assertFalse(hasattr(derived, attribute))
        with self.assertRaisesRegex(MasterKeyError, "maf_key_domain_invalid"):
            derived._consume_for(MasterKeyDomain.MCP_AUDIT_REFERENCE)
        with self.assertRaises(TypeError):
            _DerivedDomainKey(MasterKeyDomain.AUTH_TOKEN, b"x" * 32)
        with self.assertRaises(TypeError):
            pickle.dumps(derived)
        with self.assertRaises(TypeError):
            json.dumps(derived)

    def test_deriver_is_redacted_and_not_serializable(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(_ROOT_KEY)
        self.assertEqual(repr(deriver), "<MasterKeyDeriver redacted>")
        self.assertNotIn(_ROOT_KEY.hex(), repr(deriver))
        with self.assertRaises(TypeError):
            MasterKeyDeriver(_ROOT_KEY)
        with self.assertRaises(TypeError):
            pickle.dumps(deriver)


class MasterKeyFileTests(unittest.TestCase):
    def test_accepts_canonical_base64_with_optional_single_newline(self) -> None:
        for suffix in (b"", b"\n"):
            with self.subTest(suffix=suffix):
                with tempfile.TemporaryDirectory() as directory:
                    key_path = self._write_key(
                        Path(directory).resolve(), base64.b64encode(_ROOT_KEY) + suffix
                    )
                    derived = MasterKeyDeriver.from_file(key_path).derive(
                        MasterKeyDomain.MCP_CREDENTIAL
                    )
                    self.assertEqual(
                        len(derived._consume_for(MasterKeyDomain.MCP_CREDENTIAL)), 32
                    )

    def test_loaded_key_does_not_depend_on_file_after_initial_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = self._write_key(
                Path(directory).resolve(), base64.b64encode(_ROOT_KEY)
            )
            deriver = MasterKeyDeriver.from_file(key_path)
            key_path.unlink()
            self.assertEqual(
                deriver.derive(MasterKeyDomain.AUTH_TOKEN)._consume_for(
                    MasterKeyDomain.AUTH_TOKEN
                ),
                MasterKeyDeriver.from_bytes(_ROOT_KEY)
                .derive(MasterKeyDomain.AUTH_TOKEN)
                ._consume_for(MasterKeyDomain.AUTH_TOKEN),
            )

    def test_missing_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory).resolve() / "missing.key"
            self._assert_error("maf_master_key_file_missing", missing)
        self._assert_error("maf_master_key_file_missing", "")

    def test_intermediate_and_final_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real_dir = root / "real"
            real_dir.mkdir()
            key_path = self._write_key(real_dir, base64.b64encode(_ROOT_KEY))
            linked_dir = root / "linked"
            linked_dir.symlink_to(real_dir, target_is_directory=True)
            self._assert_error(
                "maf_master_key_file_invalid_type",
                linked_dir / key_path.name,
            )
            final_link = root / "linked.key"
            final_link.symlink_to(key_path)
            self._assert_error("maf_master_key_file_invalid_type", final_link)

    def test_non_regular_files_and_hardlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._assert_error("maf_master_key_file_invalid_type", root)

            fifo = root / "key.fifo"
            os.mkfifo(fifo, 0o400)
            self._assert_error("maf_master_key_file_invalid_type", fifo)

            key_path = self._write_key(root, base64.b64encode(_ROOT_KEY))
            hardlink = root / "hardlink.key"
            os.link(key_path, hardlink)
            self._assert_error("maf_master_key_file_invalid_type", key_path)
            self._assert_error("maf_master_key_file_invalid_type", hardlink)

    def test_only_0400_and_0600_permissions_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for mode in (0o000, 0o200, 0o440, 0o640, 0o644):
                key_path = self._write_key(root, base64.b64encode(_ROOT_KEY), mode=mode)
                with self.subTest(mode=oct(mode)):
                    self._assert_error(
                        "maf_master_key_file_invalid_permissions", key_path
                    )
                key_path.unlink()
            for mode in (0o400, 0o600):
                key_path = self._write_key(root, base64.b64encode(_ROOT_KEY), mode=mode)
                with self.subTest(mode=oct(mode)):
                    MasterKeyDeriver.from_file(key_path)
                key_path.unlink()

    def test_noncanonical_or_malformed_payloads_are_rejected(self) -> None:
        canonical = base64.b64encode(_ROOT_KEY)
        noncanonical = canonical[:-2] + bytes((canonical[-2] + 1,)) + canonical[-1:]
        malformed_payloads = (
            b"x" * 46,
            canonical + b"\n\n",
            canonical + b" ",
            canonical[:-1] + b"!",
            noncanonical,
            b" " + canonical[:-1],
            canonical[:-1] + b"\r",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for index, payload in enumerate(malformed_payloads):
                key_path = self._write_key(root, payload, name=f"bad-{index}.key")
                with self.subTest(payload=payload):
                    self._assert_error("maf_master_key_file_invalid_format", key_path)

    def test_canonical_base64_with_wrong_decoded_length_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = self._write_key(
                Path(directory).resolve(), base64.b64encode(b"x" * 31)
            )
            self._assert_error("maf_master_key_invalid_length", key_path)

    def test_final_directory_entry_replacement_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            key_path = self._write_key(root, base64.b64encode(_ROOT_KEY), mode=0o600)
            replacement = self._write_key(
                root,
                base64.b64encode(b"z" * 32),
                name="replacement.key",
                mode=0o600,
            )
            original_read = os.read
            replaced = False

            def replace_after_read(descriptor: int, length: int) -> bytes:
                nonlocal replaced
                payload = original_read(descriptor, length)
                if not replaced:
                    replaced = True
                    backup = root / "original.key"
                    key_path.rename(backup)
                    replacement.rename(key_path)
                return payload

            with patch(
                "src.integrations.master_key.os.read", side_effect=replace_after_read
            ):
                self._assert_error("maf_master_key_file_unavailable", key_path)

    def test_size_and_timestamp_drift_during_read_are_rejected(self) -> None:
        mutations = (
            lambda path: path.write_bytes(base64.b64encode(_ROOT_KEY) + b"\n"),
            lambda path: os.utime(
                path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1)
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    key_path = self._write_key(
                        Path(directory).resolve(),
                        base64.b64encode(_ROOT_KEY),
                        mode=0o600,
                    )
                    original_read = os.read
                    mutated = False

                    def mutate_after_read(descriptor: int, length: int) -> bytes:
                        nonlocal mutated
                        payload = original_read(descriptor, length)
                        if not mutated:
                            mutated = True
                            mutation(key_path)
                        return payload

                    with patch(
                        "src.integrations.master_key.os.read",
                        side_effect=mutate_after_read,
                    ):
                        self._assert_error("maf_master_key_file_unavailable", key_path)

    @staticmethod
    def _write_key(
        directory: Path,
        payload: bytes,
        *,
        name: str = "master.key",
        mode: int = 0o400,
    ) -> Path:
        key_path = directory / name
        key_path.write_bytes(payload)
        key_path.chmod(mode)
        return key_path

    def _assert_error(self, code: str, path: str | os.PathLike[str]) -> None:
        with self.assertRaises(MasterKeyError) as captured:
            MasterKeyDeriver.from_file(path)
        self.assertEqual(captured.exception.code, code)
        self.assertEqual(str(captured.exception), code)


if __name__ == "__main__":
    unittest.main()
