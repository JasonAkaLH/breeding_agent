from __future__ import annotations

import hashlib
import unittest

from src.storage.agent_payload import AGENT_PAYLOAD_MAX_BYTES, AgentPayloadError, canonicalize_agent_payload


class AgentPayloadTest(unittest.TestCase):
    def test_canonical_json_is_utf8_sorted_compact_and_lf_terminated(self) -> None:
        payload = canonicalize_agent_payload({"z": "中文", "a": [1, True, None]})
        self.assertEqual(payload.json_text, '{"a":[1,true,null],"z":"中文"}\n')
        self.assertEqual(payload.size_bytes, len(payload.json_text.encode("utf-8")))
        self.assertEqual(payload.sha256, hashlib.sha256(payload.json_text.encode("utf-8")).hexdigest())

    def test_exact_byte_boundaries_131071_131072_131073(self) -> None:
        overhead = len('{"v":""}\n'.encode())
        for size in (131_071, 131_072):
            payload = canonicalize_agent_payload({"v": "x" * (size - overhead)})
            self.assertEqual(payload.size_bytes, size)
        with self.assertRaisesRegex(AgentPayloadError, "too_large"):
            canonicalize_agent_payload({"v": "x" * (131_073 - overhead)})
        self.assertEqual(AGENT_PAYLOAD_MAX_BYTES, 131_072)

    def test_rejects_nan_infinity_non_string_keys_and_non_json_types(self) -> None:
        for value in ({"v": float("nan")}, {"v": float("inf")}, {1: "bad"}, {"v": {1, 2}}):
            with self.subTest(value=value), self.assertRaises(AgentPayloadError):
                canonicalize_agent_payload(value)
