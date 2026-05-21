from __future__ import annotations

import base64
import json
import unittest

from src.api.routes.uploads import _read_upload_content_with_limit
from src.api.upload_store import DEFAULT_MAX_UPLOAD_FILE_BYTES, InMemoryUploadStore, UploadValidationError
from tests.api.support import APITestCase


class _ChunkedUpload:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.read_calls = 0

    async def read(self, size: int = -1):
        self.read_calls += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class UploadReadLimitTest(unittest.IsolatedAsyncioTestCase):
    def test_default_upload_limit_is_twenty_mebibytes(self) -> None:
        self.assertEqual(DEFAULT_MAX_UPLOAD_FILE_BYTES, 20 * 1024 * 1024)
        self.assertEqual(InMemoryUploadStore().max_file_bytes, DEFAULT_MAX_UPLOAD_FILE_BYTES)

    async def test_read_upload_content_stops_as_soon_as_limit_is_exceeded(self) -> None:
        upload = _ChunkedUpload([b"12345", b"67890", b"extra-should-not-be-read"])

        with self.assertRaisesRegex(UploadValidationError, "exceeds 9 bytes"):
            await _read_upload_content_with_limit(upload, max_bytes=9, chunk_size=5)

        self.assertEqual(upload.read_calls, 2)

    async def test_read_upload_content_allows_exact_limit(self) -> None:
        upload = _ChunkedUpload([b"12345", b"67890"])

        content = await _read_upload_content_with_limit(upload, max_bytes=10, chunk_size=5)

        self.assertEqual(content, b"1234567890")
        self.assertEqual(upload.read_calls, 3)

    def test_upload_store_rejects_pathful_filename_instead_of_silent_normalization(self) -> None:
        store = InMemoryUploadStore()

        with self.assertRaisesRegex(UploadValidationError, "filename"):
            store.save(
                account_id="acc-1",
                conversation_id="conv-1",
                filename="../secret.csv",
                content_type="text/csv",
                content=b"col\nvalue\n",
            )


class UploadsAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.reconfigure_runtime(main_agent_stream_generator=lambda _prompt: ["已收到。"])

    async def test_upload_csv_returns_preview_and_submit_resolves_for_skill(self) -> None:
        csv_content = "ped_id,design_check,set\nCK_A,1,A\nA001,0,A\n"

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-upload"},
            files={"file": ("rcbd.csv", csv_content, "text/csv")},
        )

        self.assertEqual(upload.status_code, 201)
        payload = upload.json()
        self.assertEqual(payload["filename"], "rcbd.csv")
        self.assertEqual(payload["file_type"], "csv")
        self.assertEqual(payload["preview"]["columns"], ["ped_id", "design_check", "set"])
        self.assertEqual(payload["preview"]["row_count"], 2)
        self.assertNotIn("content", payload)

        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-upload", "acc-1", [payload["upload_id"]]
        )
        self.assertEqual(resolved["uploaded_artifacts"][0]["filename"], "rcbd.csv")
        self.assertNotIn("content", resolved["uploaded_artifacts"][0])
        self.assertEqual(resolved["skill_artifacts"][0]["content"], csv_content)

        submitted = await self.submit_message(
            conversation_id="conv-upload",
            content="请用这个CSV做3个区组的RCBD设计",
            capability_id=None,
            metadata={"upload_ids": [payload["upload_id"]], "blocks": 3},
        )
        self.assertEqual(submitted.status_code, 202)
        task_id = submitted.json()["task_id"]
        await self.wait_for_terminal_task(task_id)

    async def test_upload_json_returns_preview(self) -> None:
        material_data = [
            {"ped_id": "CK_A", "design_check": "1", "set": "A"},
            {"ped_id": "A001", "design_check": "0", "set": "A"},
        ]

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-json"},
            files={"file": ("materials.json", json.dumps(material_data), "application/json")},
        )

        self.assertEqual(upload.status_code, 201)
        payload = upload.json()
        self.assertEqual(payload["file_type"], "json")
        self.assertEqual(payload["preview"]["row_count"], 2)
        self.assertEqual(payload["preview"]["columns"], ["ped_id", "design_check", "set"])

    async def test_upload_png_resolves_binary_content_only_for_skill_scripts(self) -> None:
        png_content = b"\x89PNG\r\n\x1a\nocr-test"

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-ocr"},
            files={"file": ("scan.png", png_content, "image/png")},
        )

        self.assertEqual(upload.status_code, 201)
        payload = upload.json()
        self.assertEqual(payload["filename"], "scan.png")
        self.assertEqual(payload["file_type"], "image")
        self.assertEqual(payload["preview"]["shape"], "binary")
        self.assertEqual(payload["preview"]["columns"], [])
        self.assertIsNone(payload["preview"]["row_count"])
        self.assertNotIn("content", payload)
        self.assertNotIn("content_base64", payload)

        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-ocr", "acc-1", [payload["upload_id"]]
        )
        prompt_artifact = resolved["uploaded_artifacts"][0]
        script_artifact = resolved["skill_artifacts"][0]
        self.assertEqual(prompt_artifact["filename"], "scan.png")
        self.assertNotIn("content", prompt_artifact)
        self.assertNotIn("content_base64", prompt_artifact)
        self.assertEqual(script_artifact["content_base64"], base64.b64encode(png_content).decode("ascii"))
        self.assertEqual(script_artifact["encoding"], "base64")
        self.assertNotIn("content", script_artifact)

    async def test_list_and_delete_uploads_for_conversation(self) -> None:
        first = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-files"},
            files={"file": ("first.csv", "ped_id,design_check\nA,0\n", "text/csv")},
        )
        second = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-files"},
            files={"file": ("second.json", json.dumps([{"ped_id": "B", "design_check": "0"}]), "application/json")},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)

        listed = await self.client.get("/api/v1/conversations/conv-files/uploads")
        self.assertEqual(listed.status_code, 200)
        payload = listed.json()
        self.assertEqual(payload["conversation_id"], "conv-files")
        self.assertEqual([item["filename"] for item in payload["uploads"]], ["first.csv", "second.json"])
        self.assertNotIn("content", payload["uploads"][0])

        deleted = await self.client.request(
            "DELETE",
            "/api/v1/conversations/uploads",
            json={"conversation_id": "conv-files", "upload_id": first.json()["upload_id"]},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["deleted"], True)

        listed_after_delete = await self.client.get("/api/v1/conversations/conv-files/uploads")
        self.assertEqual([item["filename"] for item in listed_after_delete.json()["uploads"]], ["second.json"])

        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-files", "acc-1", [first.json()["upload_id"]]
        )
        self.assertEqual(resolved["uploaded_artifacts"], [])
        self.assertEqual(resolved["missing_upload_ids"], [first.json()["upload_id"]])

    async def test_upload_rejects_unsupported_file_type(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-upload"},
            files={"file": ("notes.txt", "hello", "text/plain")},
        )

        self.assertEqual(response.status_code, 400)

    async def test_upload_rejects_oversized_file_with_configured_limit(self) -> None:
        self.runtime.upload_store.max_file_bytes = 16

        response = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-upload"},
            files={"file": ("large.csv", "col\n" + "x" * 32, "text/csv")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("exceeds 16 bytes", response.json()["detail"])

    async def test_upload_and_reference_are_owner_scoped(self) -> None:
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-owned"},
            files={"file": ("data.csv", "ped_id,design_check\nA,0\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201)
        upload_id = upload.json()["upload_id"]

        await self.runtime.create_user("bob", "bob-password1")
        await self.login("bob", "bob-password1")

        forbidden_upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-owned"},
            files={"file": ("data.csv", "ped_id,design_check\nB,0\n", "text/csv")},
        )
        self.assertEqual(forbidden_upload.status_code, 404)

        self.runtime.upload_store.max_file_bytes = 16
        forbidden_oversized_upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-owned"},
            files={"file": ("large.csv", "col\n" + "x" * 32, "text/csv")},
        )
        self.assertEqual(forbidden_oversized_upload.status_code, 404)

        submitted = await self.submit_message(
            conversation_id="conv-owned",
            account_id="bob",
            content="用文件做RCBD",
            capability_id=None,
            metadata={"upload_ids": [upload_id], "blocks": 3},
        )
        self.assertEqual(submitted.status_code, 404)


if __name__ == "__main__":
    unittest.main()
