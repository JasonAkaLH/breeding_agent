from __future__ import annotations

import base64
import gzip
from io import BytesIO
import json
import unittest

from openpyxl import Workbook

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
                username="acc-1",
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

    async def test_upload_csv_normalizes_header_noise_and_preserves_original_hash(self) -> None:
        csv_bytes = "\ufeff\"ped_id\",hyb_check,set\nA001,0,A\n".encode("utf-8")

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-normalized-csv"},
            files={"file": ("materials.csv", csv_bytes, "text/csv")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        payload = upload.json()
        self.assertEqual(payload["preview"]["columns"], ["ped_id", "hyb_check", "set"])
        self.assertEqual(payload["preview"]["source_encoding"], "utf-8-sig")
        self.assertEqual(payload["preview"]["original_columns"][0], "ped_id")
        self.assertEqual(payload["preview"]["column_normalizations"][0]["normalized"], "ped_id")
        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-normalized-csv", "acc-1", [payload["upload_id"]]
        )
        self.assertEqual(
            resolved["skill_artifacts"][0]["content"].splitlines()[0],
            "ped_id,hyb_check,set",
        )
        self.assertEqual(resolved["skill_artifacts"][0]["original_filename"], "materials.csv")
        self.assertEqual(resolved["skill_artifacts"][0]["normalized_filename"], "materials.csv")
        self.assertNotIn("content", resolved["uploaded_artifacts"][0])

    async def test_upload_csv_accepts_legacy_encoding_and_truncates_prompt_safe_summary(self) -> None:
        columns = [f"列{i:02d}" for i in range(55)]
        rows = [",".join(columns), ",".join(f"值{i:02d}" for i in range(55))]
        csv_bytes = ("\n".join(rows) + "\n").encode("gb18030")

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-gb18030"},
            files={"file": ("materials.csv", csv_bytes, "text/csv")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        payload = upload.json()
        self.assertEqual(payload["preview"]["source_encoding"], "gb18030")
        self.assertEqual(payload["preview"]["column_count"], 55)
        self.assertEqual(len(payload["preview"]["columns"]), 50)
        self.assertTrue(payload["preview"]["columns_truncated"])
        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-gb18030", "acc-1", [payload["upload_id"]]
        )
        self.assertIn("列54", resolved["skill_artifacts"][0]["content"])
        self.assertNotIn("值54", json.dumps(resolved["uploaded_artifacts"][0], ensure_ascii=False))

    async def test_upload_rejects_duplicate_cleaned_csv_headers(self) -> None:
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-dup"},
            files={"file": ("materials.csv", "ped_id,\ufeff\"ped_id\"\nA001,A002\n", "text/csv")},
        )

        self.assertEqual(upload.status_code, 400)
        self.assertIn("duplicate", upload.json()["detail"].lower())

    async def test_upload_json_normalizes_top_level_keys_only(self) -> None:
        payload = [{"\ufeff\"ped_id\"": "A001", "nested": {"\ufeff\"ped_id\"": "raw"}}]

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-json-normalized"},
            files={"file": ("materials.json", json.dumps(payload, ensure_ascii=False), "application/json")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        body = upload.json()
        self.assertEqual(body["preview"]["columns"], ["ped_id", "nested"])
        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-json-normalized", "acc-1", [body["upload_id"]]
        )
        normalized = json.loads(resolved["skill_artifacts"][0]["content"])
        self.assertEqual(list(normalized[0].keys()), ["ped_id", "nested"])
        self.assertIn('\ufeff"ped_id"', normalized[0]["nested"])

    async def test_upload_xlsx_single_sheet_resolves_as_normalized_csv(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Only"
        sheet.append(["\ufeff\"ped_id\"", "hyb_check", "set"])
        sheet.append(["X001", 0, "A"])
        buffer = BytesIO()
        workbook.save(buffer)

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-xlsx-single"},
            files={"file": ("materials.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        payload = upload.json()
        self.assertEqual(payload["file_type"], "spreadsheet")
        self.assertEqual(payload["preview"]["selected_sheet"], "Only")
        self.assertFalse(payload["preview"]["requires_sheet_selection"])
        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-xlsx-single", "acc-1", [payload["upload_id"]]
        )
        artifact = resolved["skill_artifacts"][0]
        self.assertEqual(artifact["filename"], "materials.csv")
        self.assertEqual(artifact["original_filename"], "materials.xlsx")
        self.assertEqual(artifact["content"].splitlines()[0], "ped_id,hyb_check,set")

    async def test_upload_xls_single_sheet_resolves_as_normalized_csv(self) -> None:
        xls_bytes = base64.b64decode(_SINGLE_SHEET_XLS_BASE64)

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-xls-single"},
            files={"file": ("materials.xls", xls_bytes, "application/vnd.ms-excel")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        payload = upload.json()
        self.assertEqual(payload["file_type"], "spreadsheet")
        self.assertEqual(payload["preview"]["selected_sheet"], "Only")
        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-xls-single", "acc-1", [payload["upload_id"]]
        )
        self.assertEqual(resolved["skill_artifacts"][0]["content"].splitlines()[0], "ped_id,hyb_check,set")

    async def test_upload_xlsx_multi_sheet_requires_selection_without_executable_content(self) -> None:
        workbook = Workbook()
        workbook.active.title = "A"
        workbook.active.append(["ped_id", "hyb_check"])
        workbook.active.append(["A001", 0])
        sheet_b = workbook.create_sheet("B")
        sheet_b.append(["ped_id", "hyb_check"])
        sheet_b.append(["B001", 1])
        buffer = BytesIO()
        workbook.save(buffer)

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-xlsx-multi"},
            files={"file": ("materials.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        payload = upload.json()
        self.assertEqual(payload["file_type"], "spreadsheet")
        self.assertTrue(payload["preview"]["requires_sheet_selection"])
        self.assertEqual([item["sheet_name"] for item in payload["preview"]["excel_sheets"]], ["A", "B"])
        unresolved = await self.runtime.resolve_uploads_for_message(
            "conv-xlsx-multi", "acc-1", [payload["upload_id"]]
        )
        self.assertEqual(len(unresolved["pending_sheet_selections"]), 1)
        self.assertNotIn("content", unresolved["skill_artifacts"][0])
        self.assertNotIn("content_base64", unresolved["skill_artifacts"][0])
        stored_record = self.runtime.upload_store.get_for_message(
            upload_id=payload["upload_id"],
            username="acc-1",
            conversation_id="conv-xlsx-multi",
        )
        direct_artifact = stored_record.to_skill_artifact()
        self.assertNotIn("content", direct_artifact)
        self.assertNotIn("content_base64", direct_artifact)
        selected = await self.runtime.resolve_uploads_for_message(
            "conv-xlsx-multi",
            "acc-1",
            [payload["upload_id"]],
            upload_sheet_selections={payload["upload_id"]: "B"},
        )
        self.assertEqual(selected["pending_sheet_selections"], [])
        self.assertIn("B001", selected["skill_artifacts"][0]["content"])

    async def test_submit_multi_sheet_upload_creates_sheet_selection_interrupt_and_resume_accepts_choice(self) -> None:
        workbook = Workbook()
        workbook.active.title = "Alpha"
        workbook.active.append(["ped_id", "hyb_check"])
        workbook.active.append(["A001", 0])
        beta = workbook.create_sheet("Beta")
        beta.append(["ped_id", "hyb_check"])
        beta.append(["B001", 1])
        buffer = BytesIO()
        workbook.save(buffer)
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-sheet-interrupt"},
            files={"file": ("materials.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]

        submitted = await self.submit_message(
            conversation_id="conv-sheet-interrupt",
            content="用这个多 sheet 文件做设计",
            capability_id=None,
            metadata={"upload_ids": [upload_id]},
        )

        self.assertEqual(submitted.status_code, 202, submitted.text)
        task_id = submitted.json()["task_id"]
        await self.wait_for_condition(
            lambda: self.runtime.storage.list_interrupts_for_task(task_id),
            timeout=3,
        )
        interrupts = await self.runtime.list_interrupts(task_id)
        open_interrupt = next(item for item in interrupts if item["status"] == "open")
        self.assertEqual(open_interrupt["reason_code"], "sheet_selection_required")
        sheet_field = open_interrupt["required_fields"]["upload_sheet_selections"]
        self.assertEqual(sheet_field["required_upload_ids"], [upload_id])
        self.assertEqual(sheet_field["options_by_upload_id"][upload_id], ["Alpha", "Beta"])
        self.assertNotIn("A001", json.dumps(sheet_field, ensure_ascii=False))
        events = await self.runtime.storage.list_events_for_task(task_id)
        waiting_events = [event for event in events if event.event_type == "node.waiting_for_input"]
        self.assertEqual(len(waiting_events), 1)
        self.assertEqual(waiting_events[0].node_id, open_interrupt["node_id"])
        self.assertEqual(waiting_events[0].payload["interrupt_id"], open_interrupt["interrupt_id"])
        self.assertEqual(waiting_events[0].payload["reason_code"], open_interrupt["reason_code"])

        invalid = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": open_interrupt["interrupt_id"],
                "answer_payload": {"upload_sheet_selections": {upload_id: "Missing"}},
            },
        )
        self.assertEqual(invalid.status_code, 400)

        # Re-open the scenario because the invalid answer is intentionally fail-closed.
        second = await self.submit_message(
            conversation_id="conv-sheet-interrupt-2",
            content="用这个多 sheet 文件做设计",
            capability_id=None,
            metadata={"upload_ids": [upload_id]},
        )
        self.assertEqual(second.status_code, 404, "upload is conversation scoped and must not be reusable elsewhere")

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": open_interrupt["interrupt_id"],
                "answer_payload": {"upload_sheet_selections": {upload_id: "Beta"}},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        await self.runtime._await_existing_execution(task_id)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

    async def test_sheet_selection_resume_uses_task_bound_attachment_when_staged_upload_is_gone(self) -> None:
        workbook = Workbook()
        workbook.active.title = "Alpha"
        workbook.active.append(["ped_id", "hyb_check"])
        workbook.active.append(["A001", 0])
        beta = workbook.create_sheet("Beta")
        beta.append(["ped_id", "hyb_check"])
        beta.append(["B001", 1])
        buffer = BytesIO()
        workbook.save(buffer)
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-sheet-expired"},
            files={"file": ("materials.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]

        submitted = await self.submit_message(
            conversation_id="conv-sheet-expired",
            content="用这个多 sheet 文件做设计",
            capability_id=None,
            metadata={"upload_ids": [upload_id]},
        )
        self.assertEqual(submitted.status_code, 202, submitted.text)
        task_id = submitted.json()["task_id"]
        await self.wait_for_condition(
            lambda: self.runtime.storage.list_interrupts_for_task(task_id),
            timeout=3,
        )
        open_interrupt = next(item for item in await self.runtime.list_interrupts(task_id) if item["status"] == "open")
        deleted = self.runtime.upload_store.delete(
            upload_id=upload_id,
            username="acc-1",
            conversation_id="conv-sheet-expired",
        )
        self.assertTrue(deleted)

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": open_interrupt["interrupt_id"],
                "answer_payload": {"upload_sheet_selections": {upload_id: "Beta"}},
            },
        )

        self.assertEqual(answer.status_code, 202, answer.text)
        await self.runtime._await_existing_execution(task_id)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        interrupts_after = await self.runtime.list_interrupts(task_id)
        self.assertEqual([item["status"] for item in interrupts_after], ["answered"])
        node_after = await self.runtime.storage.get_task_node(open_interrupt["node_id"])
        self.assertIsNotNone(node_after)
        self.assertNotEqual(str(node_after.status), "waiting_for_input")
        saved_answers = await self.runtime.storage.list_interrupt_answers(open_interrupt["interrupt_id"])
        self.assertEqual(len(saved_answers), 1)
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].source_kind, "message_upload")
        self.assertEqual(attachments[0].selected_sheet, "Beta")
        self.assertEqual(attachments[0].interrupt_answer_id, saved_answers[0].interrupt_answer_id)
        self.assertIn("content", attachments[0].skill_artifact)
        self.assertNotIn("content", attachments[0].prompt_artifact)

    async def test_vnd_ms_excel_without_excel_magic_stays_csv_compatible(self) -> None:
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-vnd-csv"},
            files={"file": ("materials", "ped_id\nA001\n", "application/vnd.ms-excel")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        self.assertEqual(upload.json()["file_type"], "csv")

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

    async def test_upload_vcf_resolves_binary_content_only_for_skill_scripts(self) -> None:
        vcf_content = (
            b"##fileformat=VCFv4.2\n"
            b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample_1\n"
            b"1\t42\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n"
        )
        self.runtime.upload_store.max_preview_bytes = 8

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-rice-vcf"},
            files={"file": ("sample.vcf", vcf_content, "text/plain")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        payload = upload.json()
        self.assertEqual(payload["filename"], "sample.vcf")
        self.assertEqual(payload["file_type"], "vcf")
        self.assertEqual(payload["preview"]["shape"], "binary")
        self.assertIsNone(payload["preview"]["row_count"])
        self.assertNotIn("content", payload)
        self.assertNotIn("content_base64", payload)

        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-rice-vcf", "acc-1", [payload["upload_id"]]
        )
        prompt_artifact = resolved["uploaded_artifacts"][0]
        script_artifact = resolved["skill_artifacts"][0]
        self.assertEqual(prompt_artifact["filename"], "sample.vcf")
        self.assertEqual(prompt_artifact["file_type"], "vcf")
        self.assertNotIn("content", prompt_artifact)
        self.assertNotIn("content_base64", prompt_artifact)
        self.assertEqual(script_artifact["filename"], "sample.vcf")
        self.assertEqual(script_artifact["content_base64"], base64.b64encode(vcf_content).decode("ascii"))
        self.assertEqual(script_artifact["encoding"], "base64")
        self.assertNotIn("content", script_artifact)

    async def test_upload_vcf_gz_resolves_by_compound_extension_without_accepting_plain_gz(self) -> None:
        compressed_vcf = gzip.compress(
            b"##fileformat=VCFv4.2\n"
            b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample_1\n"
            b"1\t42\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n"
        )

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-rice-vcf-gz"},
            files={"file": ("sample.vcf.gz", compressed_vcf, "application/gzip")},
        )

        self.assertEqual(upload.status_code, 201, upload.text)
        payload = upload.json()
        self.assertEqual(payload["filename"], "sample.vcf.gz")
        self.assertEqual(payload["file_type"], "vcf")
        resolved = await self.runtime.resolve_uploads_for_message(
            "conv-rice-vcf-gz", "acc-1", [payload["upload_id"]]
        )
        self.assertEqual(resolved["skill_artifacts"][0]["filename"], "sample.vcf.gz")
        self.assertEqual(
            resolved["skill_artifacts"][0]["content_base64"],
            base64.b64encode(compressed_vcf).decode("ascii"),
        )

        plain_gz = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-rice-vcf-gz"},
            files={"file": ("archive.gz", compressed_vcf, "application/gzip")},
        )
        self.assertEqual(plain_gz.status_code, 400)

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

    async def test_submit_with_missing_upload_id_fails_closed_before_creating_task(self) -> None:
        submitted = await self.submit_message(
            conversation_id="conv-missing-upload-submit",
            content="用这个不存在的文件做设计",
            capability_id=None,
            metadata={"upload_ids": ["upl-missing"]},
        )

        self.assertEqual(submitted.status_code, 400)
        self.assertIn("Missing or expired uploads", submitted.json()["detail"])
        self.assertIsNone(await self.runtime.storage.get_conversation("conv-missing-upload-submit"))

    async def test_missing_conversation_upload_list_and_unknown_delete_are_documented_noops(self) -> None:
        listed = await self.client.get("/api/v1/conversations/missing-conversation/uploads")

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            listed.json(),
            {"conversation_id": "missing-conversation", "uploads": []},
        )

        deleted = await self.client.request(
            "DELETE",
            "/api/v1/conversations/uploads",
            json={"conversation_id": "missing-conversation", "upload_id": "upl-missing"},
        )

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"upload_id": "upl-missing", "deleted": False})

    async def test_upload_rejects_unsupported_file_type(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-upload"},
            files={"file": ("notes.txt", "hello", "text/plain")},
        )

        self.assertEqual(response.status_code, 400)

    async def test_upload_rejects_extra_identity_and_auth_form_fields(self) -> None:
        for field_name in ("username", "auth_token", "session", "captcha", "password", "unexpected"):
            with self.subTest(field_name=field_name):
                response = await self.client.post(
                    "/api/v1/conversations/uploads",
                    data={"conversation_id": "conv-upload", field_name: "spoof"},
                    files={"file": ("materials.csv", "ped_id,design_check\nA,0\n", "text/csv")},
                )

                self.assertEqual(response.status_code, 422, response.text)

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

        await self.login("bob")

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
            content="用文件做RCBD",
            capability_id=None,
            metadata={"upload_ids": [upload_id], "blocks": 3},
        )
        self.assertEqual(submitted.status_code, 404)


if __name__ == "__main__":
    unittest.main()


_SINGLE_SHEET_XLS_BASE64 = (
    '0M8R4KGxGuEAAAAAAAAAAAAAAAAAAAAAPgADAP7/CQAGAAAAAAAAAAAAAAABAAAACQAAAAAAAAAAEAAA/v///wAAAAD+////'
    'AAAAAAgAAAD/////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '//////////8JCBAAAAYFALsNzAcAAAAABgAAAOEAAgCwBMEAAgAAAOIAAABcAHAATm9uZSAgICAgICAgICAgICAgICAgICAg'
    'ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg'
    'ICAgICAgICAgICAgICAgIEIAAgCwBGEBAgAAAD0BAgABAJwAAgAOABkAAgAAABIAAgAAAGMAAgAAABMAAgAAAK8BAgAAALwB'
    'AgAAAEAAAgAAAI0AAgAAAD0AEgDgAVoAzz9OKjgAAAAAAAEAWAIiAAIAAAAOAAIAAQC3AQIAAADaAAIAAAAxABUAyAAAAP9/'
    'kAEAAAAAAQAFAEFyaWFsMQAVAMgAAAD/f5ABAAAAAAEABQBBcmlhbDEAFQDIAAAA/3+QAQAAAAABAAUAQXJpYWwxABUAyAAA'
    'AP9/kAEAAAAAAQAFAEFyaWFsMQAVAMgAAAD/f5ABAAAAAAEABQBBcmlhbDEAFQDIAAAA/3+QAQAAAAABAAUAQXJpYWwxABUA'
    'yAAAAP9/kAEAAAAAAQAFAEFyaWFsHgQMAKQABwAAR2VuZXJhbOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8g'
    'AAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8g'
    'AAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8g'
    'AAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8g'
    'AAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8g'
    'AAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQA9f8gAAD0AAAAAAAAAADAIOAAFAAGAKQAAQAg'
    'AAD4AAAAAAAAAADAIOAAFAAHAKQAAQAgAAD4AAAAAAAAAADAIJMCBAAAgAD/YAECAAEAhQAMANEDAAAAAAQAT25sefwALgAF'
    'AAAABQAAAAYAAHBlZF9pZAkAAGh5Yl9jaGVjawMAAHNldAQAAFgwMDEBAABBCgAAAAkIEAAABhAAuw3MBwAAAAAGAAAADQAC'
    'AAEADAACAGQADwACAAEAEQACAAAAEAAIAPyp8dJNYlA/XwACAAAAgAAIAAAAAAABAAAAJQIEAAAA/wCBAAIAAQwAAg4AAAAA'
    'AAIAAAAAAAMAAAAqAAIAAAArAAIAAACCAAIAAQAbAAIAAAAaAAIAAAAUAAUAAgAAJlAVAAUAAgAAJkaDAAIAAQCEAAIAAAAm'
    'AAgAMzMzMzMz0z8nAAgAMzMzMzMz0z8oAAgAhetRuB6F4z8pAAgArkfhehSu1z+hACIACQBkAAEAAQABAIMALAEsAZqZmZmZ'
    'mbk/mpmZmZmZuT8BABIAAgAAAN0AAgAAABkAAgAAAGMAAgAAABMAAgAAAAgCEAAAAAAAAwD/AAAAAAAAAQ8A/QAKAAAAAAAR'
    'AAAAAAD9AAoAAAABABEAAQAAAP0ACgAAAAIAEQACAAAACAIQAAEAAAADAP8AAAAAAAABDwD9AAoAAQAAABEAAwAAAH4CCgAB'
    'AAEAEQACAAAA/QAKAAEAAgARAAQAAAA+AhIAtgIAAAAAQAAAAAAAAAAAAAAACgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AQAAAAIAAAADAAAABAAAAAUAAAAGAAAABwAAAP7////9/////v//////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////'
    '//////////9SAG8AbwB0ACAARQBuAHQAcgB5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'FgAFAf//////////AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP7///8AAAAAAAAAAFcAbwByAGsA'
    'YgBvAG8AawAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASAAIB////////////////'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH///////////////8AAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAD+////AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAf///////////////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAP7///8AAAAAAAAAAA=='
)
