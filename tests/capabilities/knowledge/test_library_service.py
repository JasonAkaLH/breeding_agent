from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.capabilities.knowledge import KnowledgeError, KnowledgeService
from src.capabilities.knowledge.service import document_text_version
from src.storage.knowledge_files import KnowledgeFileStore
from src.storage.knowledge_repository import KnowledgeRepository
from src.storage.sqlite.bootstrap import bootstrap_sqlite_database
from src.storage.sqlite.session import create_sqlite_engine, create_sqlite_session_factory


async def extract(text):
    return {"title": "Paper", "summary": "全文方法与发现 " + text[-20:]}


class KnowledgeServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_pdf_text_version_binds_both_hashes_and_markdown_remains_legacy(self):
        source = {"body_ref": "lib/id/source.pdf", "sha256": "a" * 64,
                  "parsed_body_ref": "lib/id/parsed.md", "parsed_body_sha256": "b" * 64}
        version = document_text_version(source)
        self.assertEqual(len(version), 64)
        for field in ("sha256", "parsed_body_sha256"):
            self.assertNotEqual(document_text_version({**source, field: "c" * 64}), version)
        self.assertEqual(document_text_version({**source, "body_ref": "lib/id/source.md"}), source["sha256"])

    async def pdf_draft(self, raw=None, **kwargs):
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        raw = raw or text_pdf("Full evidence", "Final evidence")
        item = (await self.service.upload_imports("alice", self.library_id, [("paper.pdf", raw)], **kwargs))[0]
        self.assertIn("import_id", item)
        await self.service.process_pending()
        return await self.service.get_import("alice", self.library_id, item["import_id"])

    async def test_all_four_format_replacements_keep_id_and_clean_both_files(self):
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        document = await self.published()
        for filename, raw in [("one.pdf", text_pdf("One")), ("two.pdf", text_pdf("Two")), ("three.md", b"# Three"), ("four.md", b"# Four")]:
            with self.subTest(filename=filename):
                old_refs = [ref for ref in (document["body_ref"], document["parsed_body_ref"]) if ref]
                item = (await self.service.upload_imports("alice", self.library_id, [(filename, raw)], replacement_document_id=document["document_id"]))[0]
                await self.service.process_pending()
                current = await self.service.confirm_import("alice", self.library_id, item["import_id"])
                self.assertEqual(current["status"], "published")
                self.assertEqual(current["document_id"], document["document_id"])
                document = self.repo.get_document(document["document_id"])
                self.assertEqual((await self.service.download_document("alice", self.library_id, document["document_id"]))["content"], raw)
                for ref in old_refs:
                    with self.assertRaises(FileNotFoundError):
                        self.files.read_body(ref)
                with self.assertRaises(KnowledgeError):
                    await self.service.download_import_source("alice", self.library_id, item["import_id"])

    async def test_pdf_dedup_uses_source_bytes_and_cross_library_is_independent(self):
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        raw = text_pdf("Identical parsed text")
        items = await asyncio.gather(*(self.service.upload_imports("alice", self.library_id, [("a.pdf", raw)]) for _ in range(3)))
        self.assertEqual(len({items[0]["import_id"] for items in items}), 1)
        await self.service.process_pending()
        first = await self.service.confirm_import("alice", self.library_id, items[0][0]["import_id"])
        duplicate = (await self.service.upload_imports("alice", self.library_id, [("different.pdf", raw)]))[0]
        self.assertEqual(duplicate["document_id"], first["document_id"])
        changed_source = raw + b"\n% Source-only change\n"
        second = await self.pdf_draft(changed_source)
        other = await self.service.confirm_import("alice", self.library_id, second["import_id"])
        read1 = await self.service.read_document("alice", self.library_id, first["document_id"])
        read2 = await self.service.read_document("alice", self.library_id, other["document_id"])
        self.assertEqual(read1["sha256"], read2["sha256"])
        self.assertNotEqual(read1["version"], read2["version"])
        independent = await self.service.create_library("alice", "Independent")
        uploaded = (await self.service.upload_imports("alice", independent["library_id"], [("same.pdf", raw)]))[0]
        self.assertNotIn("duplicate_type", uploaded)

    async def test_pdf_parse_failure_never_reaches_extractor_or_manual_confirmation(self):
        from tests.capabilities.knowledge.pdf_fixtures import unreadable_page_pdf
        from unittest.mock import AsyncMock

        self.service.extractor = AsyncMock()
        for raw, error in [(unreadable_page_pdf(mixed=True), "pdf_page_text_unavailable"), (b"%PDF-broken", "pdf_unreadable")]:
            draft = await self.pdf_draft(raw)
            self.assertEqual(draft["error_code"], error)
            self.assertFalse(draft["text_ready"])
            with self.assertRaises(KnowledgeError):
                await self.service.edit_import("alice", self.library_id, draft["import_id"], {"title": "Manual", "summary": "Manual"}, expected_revision=draft["revision"])
            with self.assertRaises(KnowledgeError):
                await self.service.confirm_import("alice", self.library_id, draft["import_id"])
        self.service.extractor.assert_not_called()

    async def test_registered_missing_text_recovers_and_metadata_failure_is_reviewable(self):
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        item = (await self.service.upload_imports("alice", self.library_id, [("a.pdf", text_pdf("Recover text"))]))[0]
        original_put = self.files.put_body
        with patch.object(self.files, "put_body", side_effect=OSError("PRIVATE internal content")):
            await self.service.process_pending()
        failed = self.repo.get_import(item["import_id"])
        missing_ref = failed["parsed_body_ref"]
        self.assertTrue(missing_ref)
        self.assertFalse((await self.service.get_import("alice", self.library_id, item["import_id"]))["text_ready"])
        await self.service.retry_import("alice", self.library_id, item["import_id"])
        async def failed_metadata(_):
            raise RuntimeError("PRIVATE reference body")
        self.service.extractor = failed_metadata
        self.assertEqual(self.files.put_body, original_put)
        await self.service.process_pending()
        draft = await self.service.get_import("alice", self.library_id, item["import_id"])
        self.assertEqual(draft["error_code"], "extraction_failed")
        self.assertTrue(draft["text_ready"])
        edited = await self.service.edit_import("alice", self.library_id, item["import_id"], {"title": "Reviewed", "summary": "Reviewed full evidence"}, expected_revision=draft["revision"])
        result = await self.service.confirm_import("alice", self.library_id, item["import_id"], expected_revision=edited["revision"])
        self.assertEqual(result["status"], "published")
        with self.assertRaises(FileNotFoundError):
            self.files.read_body(missing_ref)

    async def test_discard_while_registered_text_write_is_late_cannot_leave_a_copy(self):
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        item = (await self.service.upload_imports("alice", self.library_id, [("slow.pdf", text_pdf("Late write"))]))[0]
        started, finish = threading.Event(), threading.Event()
        put = self.files.put_body
        def slow(*args, **kwargs):
            started.set()
            if not finish.wait(5):
                raise AssertionError("Write test release timed out")
            return put(*args, **kwargs)
        with patch.object(self.files, "put_body", side_effect=slow):
            processing = asyncio.create_task(self.service.process_pending())
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            current = self.repo.get_import(item["import_id"])
            self.assertTrue(current["parsed_body_ref"])
            try:
                await self.service.discard_import("alice", self.library_id, item["import_id"])
            finally:
                finish.set()
            await processing
        row = self.repo.get_import(item["import_id"])
        self.assertEqual(row["status"], "discarded")
        self.assertFalse(row["cleanup_pending"])
        for ref in (row["body_ref"], row["parsed_body_ref"]):
            with self.assertRaises(FileNotFoundError):
                self.files.read_body(ref)

    async def test_parser_claim_takeover_and_cancellation_do_not_publish_late_text(self):
        from src.capabilities.knowledge.pdf import parse_pdf
        from src.storage.sqlalchemy_models import KnowledgeImportRow
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        for cancel in (False, True):
            started, finish, finished = threading.Event(), threading.Event(), threading.Event()
            def slow(raw):
                started.set()
                if not finish.wait(5):
                    raise AssertionError("Parser release timed out")
                try:
                    return parse_pdf(raw)
                finally:
                    finished.set()
            item = (await self.service.upload_imports("alice", self.library_id, [("slow.pdf", text_pdf(str(cancel)))]))[0]
            with patch("src.capabilities.knowledge.service.parse_pdf", side_effect=slow):
                processing = asyncio.create_task(self.service.process_pending())
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                if cancel:
                    processing.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await processing
                else:
                    with self.repo.session_factory() as session:
                        session.get(KnowledgeImportRow, item["import_id"]).claim_expires_at = 0
                        session.commit()
                    adopted = self.repo.claim_import("replacement-worker")
                    self.assertEqual(adopted["import_id"], item["import_id"])
                finish.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 3))
                if not cancel:
                    await processing
            current = self.repo.get_import(item["import_id"])
            self.assertIsNone(current["parsed_body_ref"])
            if not cancel:
                self.assertEqual(current["claim_token"], adopted["claim_token"])
                with self.repo.session_factory() as session:
                    session.get(KnowledgeImportRow, item["import_id"]).claim_expires_at = 0
                    session.commit()
            await self.service.process_pending()
            self.assertEqual(self.repo.get_import(item["import_id"])["status"], "awaiting_review")
            await self.service.discard_import("alice", self.library_id, item["import_id"])

    async def test_late_parser_cannot_write_after_discard_or_library_delete(self):
        from src.capabilities.knowledge.pdf import parse_pdf
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        for delete_library in (False, True):
            started, finish = threading.Event(), threading.Event()
            def slow(raw):
                started.set()
                if not finish.wait(5):
                    raise AssertionError("Parser test release timed out")
                return parse_pdf(raw)
            item = (await self.service.upload_imports("alice", self.library_id, [("slow.pdf", text_pdf(str(delete_library)))]))[0]
            with patch("src.capabilities.knowledge.service.parse_pdf", side_effect=slow):
                processing = asyncio.create_task(self.service.process_pending())
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                try:
                    if delete_library:
                        await self.service.delete_library("alice", self.library_id)
                    else:
                        await self.service.discard_import("alice", self.library_id, item["import_id"])
                finally:
                    finish.set()
                await processing
            row = self.repo.get_import(item["import_id"])
            self.assertEqual(row["status"], "discarded")
            self.assertIsNone(row["parsed_body_ref"])
            with self.assertRaises(FileNotFoundError):
                self.files.read_body(row["body_ref"])

    async def test_pdf_source_text_gate_and_pair_cleanup(self):
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        raw = text_pdf("FIRST full text", "LAST detail")
        item = (await self.service.upload_imports("alice", self.library_id, [("paper.pdf", raw)]))[0]
        self.assertEqual(item["status"], "pending")
        with self.assertRaises(KnowledgeError):
            await self.service.edit_import("alice", self.library_id, item["import_id"], {"title": "Manual", "summary": "Manual"}, expected_revision=item["revision"])
        await self.service.process_pending()
        draft = await self.service.get_import("alice", self.library_id, item["import_id"])
        self.assertTrue(draft["text_ready"])
        self.assertEqual((await self.service.download_import_source("alice", self.library_id, item["import_id"]))["content"], raw)
        preview = await self.service.preview_import("alice", self.library_id, item["import_id"])
        self.assertIn("LAST detail", preview["text"])
        published = await self.service.confirm_import("alice", self.library_id, item["import_id"])
        document = self.repo.get_document(published["document_id"])
        source = await self.service.download_document("alice", self.library_id, document["document_id"])
        text = await self.service.read_document("alice", self.library_id, document["document_id"])
        self.assertEqual(source["content"], raw)
        self.assertEqual(text["sha256"], document["parsed_body_sha256"])
        self.assertNotEqual(text["version"], text["sha256"])
        with self.assertRaises(KnowledgeError):
            await self.service.preview_import("alice", self.library_id, item["import_id"])
        await self.service.delete_document("alice", self.library_id, document["document_id"])
        for ref in (document["body_ref"], document["parsed_body_ref"]):
            with self.assertRaises(FileNotFoundError):
                self.files.read_body(ref)

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.directory.name) / "state.db")
        bootstrap_sqlite_database(self.engine)
        self.repo = KnowledgeRepository(create_sqlite_session_factory(self.engine))
        self.files = KnowledgeFileStore(Path(self.directory.name) / "knowledge")
        self.service = KnowledgeService(self.repo, self.files, extractor=extract, user_exists=lambda user: user in {"alice", "bob"})
        self.library = await self.service.create_library("alice", "育种", "研究育种")
        self.library_id = self.library["library_id"]

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.engine.dispose()
        self.directory.cleanup()

    async def draft(self, raw=b"# Paper\nOutside abstract detail", library_id=None, **kwargs):
        items = await self.service.upload_imports("alice", library_id or self.library_id, [("paper.md", raw)], **kwargs)
        self.assertIsNone(items[0].get("error"))
        await self.service.process_pending()
        return await self.service.get_import("alice", library_id or self.library_id, items[0]["import_id"])

    async def published(self, raw=b"# Paper\nOutside abstract detail", library_id=None):
        imported = await self.draft(raw, library_id)
        result = await self.service.confirm_import("alice", library_id or self.library_id, imported["import_id"], expected_revision=imported["revision"])
        self.assertEqual(result["status"], "published")
        return self.repo.get_document(result["document_id"])

    async def test_manual_review_raw_bytes_metadata_index_and_sharing(self):
        raw = b"\xef\xbb\xbf# Paper\r\nmethod outside abstract  \r\n"
        draft = await self.draft(raw)
        self.assertEqual(draft["status"], "awaiting_review")
        self.assertEqual(await self.service.list_documents("alice", self.library_id), [])
        self.assertNotIn("Paper", (await self.service.read_index("alice", self.library_id))["text"])
        grant = await self.service.add_grant("alice", self.library_id, "bob")
        self.assertEqual(grant, await self.service.add_grant("alice", self.library_id, "bob"))
        with self.assertRaises(KnowledgeError) as denied:
            await self.service.list_imports("bob", self.library_id)
        self.assertEqual(denied.exception.status_code, 403)
        edited = await self.service.edit_import("alice", self.library_id, draft["import_id"], {"title": "Reviewed", "summary": "Confirmed full-text finding"}, expected_revision=draft["revision"])
        result = await self.service.confirm_import("alice", self.library_id, draft["import_id"], expected_revision=edited["revision"])
        document_id = result["document_id"]
        downloaded = await self.service.download_document("bob", self.library_id, document_id)
        self.assertEqual(downloaded["content"], raw)
        self.assertEqual(downloaded["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(downloaded["metadata"]["authors"], [])
        index = (await self.service.read_index("bob", self.library_id))["text"]
        self.assertIn("Reviewed", index)
        self.assertIn("Confirmed full-text finding", index)
        self.assertNotIn(str(self.files.root_dir), index)
        for operation in (
            self.service.update_library("bob", self.library_id, name="bad", expected_revision=self.library["revision"]),
            self.service.delete_document("bob", self.library_id, document_id),
            self.service.add_grant("bob", self.library_id, "alice"),
        ):
            with self.assertRaises(KnowledgeError):
                await operation
        await self.service.revoke_grant("alice", self.library_id, grant["grant_id"])
        with self.assertRaises(KnowledgeError):
            await self.service.download_document("bob", self.library_id, document_id)
        with self.assertRaises(KnowledgeError):
            await self.service.add_grant("alice", self.library_id, "newuser")
        with self.assertRaises(KnowledgeError):
            await self.service.add_grant("alice", self.library_id, "team1", subject_type="team")

    async def test_success_logs_expose_states_counts_and_duration_without_reference_text(self):
        raw = b"# PRIVATE_BODY_MARKER\nconfidential experimental details"
        with self.assertLogs("src.capabilities.knowledge.service", level="INFO") as captured:
            draft = await self.draft(raw)
            reviewed = await self.service.edit_import(
                "alice", self.library_id, draft["import_id"],
                {"title": "PRIVATE_TITLE_MARKER", "summary": "PRIVATE_SUMMARY_MARKER"},
                expected_revision=draft["revision"],
            )
            published = await self.service.confirm_import(
                "alice", self.library_id, draft["import_id"], expected_revision=reviewed["revision"],
            )
            await self.service.add_grant("alice", self.library_id, "bob")
            await self.service.delete_document("alice", self.library_id, published["document_id"])
        records = {record.msg: record for record in captured.records}
        self.assertEqual(records["knowledge_upload_batch_completed"].count, 1)
        self.assertGreaterEqual(records["knowledge_extraction_completed"].duration_ms, 0)
        self.assertEqual(records["knowledge_extraction_completed"].status, "awaiting_review")
        self.assertEqual(records["knowledge_review_saved"].status, "awaiting_review")
        self.assertEqual(records["knowledge_confirmation_processed"].status, "published")
        self.assertEqual(records["knowledge_grant_saved"].status, "reader")
        self.assertEqual(records["knowledge_document_cleanup_completed"].status, "complete")
        serialized_records = str([record.__dict__ for record in captured.records])
        for private_value in ("PRIVATE_BODY_MARKER", "PRIVATE_TITLE_MARKER", "PRIVATE_SUMMARY_MARKER", "paper.md", str(self.files.root_dir)):
            self.assertNotIn(private_value, serialized_records)

    async def test_dedup_batch_cross_library_and_byte_identity(self):
        raw = b"# A\r\nx"
        items = await self.service.upload_imports("alice", self.library_id, [("a.md", raw), ("other.md", raw), ("a.txt", b"bad"), ("a.md", b"# A\nx")])
        self.assertEqual(items[0]["import_id"], items[1]["import_id"])
        self.assertEqual(items[1]["duplicate_type"], "import")
        self.assertEqual(items[2]["status"], "failed")
        self.assertNotEqual(items[0]["import_id"], items[3]["import_id"])
        await self.service.process_pending()
        results = await self.service.batch_confirm("alice", self.library_id, [{"import_id": items[0]["import_id"]}, {"import_id": "missing"}])
        self.assertEqual([item["status"] for item in results], ["published", "failed"])
        duplicated = (await self.service.upload_imports("alice", self.library_id, [("again.md", raw)]))[0]
        self.assertEqual(duplicated["duplicate_type"], "document")
        other = await self.service.create_library("alice", "other")
        independent = await self.published(raw, other["library_id"])
        await self.service.delete_document("alice", self.library_id, duplicated["document_id"])
        self.assertEqual((await self.service.download_document("alice", other["library_id"], independent["document_id"]))["content"], raw)
        again = (await self.service.upload_imports("alice", self.library_id, [("again.md", raw)]))[0]
        self.assertNotEqual(again["import_id"], items[0]["import_id"])

    async def test_replacement_deletes_old_bytes_and_recovers_cleanup_failure(self):
        document = await self.published()
        old_ref = document["body_ref"]
        same = (await self.service.upload_imports("alice", self.library_id, [("new.md", self.files.read_body(old_ref))], replacement_document_id=document["document_id"]))[0]
        self.assertTrue(same["unchanged"])
        draft = await self.draft(b"# New\nnew detail", replacement_document_id=document["document_id"])
        self.assertEqual((await self.service.read_document("alice", self.library_id, document["document_id"]))["sha256"], document["sha256"])
        with patch.object(self.files, "delete_body", side_effect=OSError("private body should not be logged")):
            result = await self.service.confirm_import("alice", self.library_id, draft["import_id"])
        self.assertEqual(result["status"], "publishing")
        self.assertEqual(result["error_code"], "publication_incomplete")
        with self.assertRaises(KnowledgeError):
            await self.service.read_document("alice", self.library_id, document["document_id"])
        with self.assertRaises(KnowledgeError) as incomplete_index:
            await self.service.read_index("alice", self.library_id)
        self.assertEqual(incomplete_index.exception.code, "publication_incomplete")
        self.assertEqual(incomplete_index.exception.status_code, 409)
        restarted = KnowledgeService(self.repo, self.files, extractor=extract)
        await restarted.process_pending()
        current = self.repo.get_document(document["document_id"])
        self.assertEqual(current["publication_state"], "complete")
        self.assertEqual(current["document_id"], document["document_id"])
        with self.assertRaises(FileNotFoundError):
            self.files.read_body(old_ref)
        self.assertEqual((await restarted.read_document("alice", self.library_id, document["document_id"]))["text"], "# New\nnew detail")
        repaired_index = await restarted.read_index("alice", self.library_id)
        self.assertIn(current["document_id"], repaired_index["text"])
        self.assertIn(current["summary"], repaired_index["text"])

    async def test_index_failure_repair_never_serves_old_index(self):
        document = await self.published()
        with patch.object(self.files, "publish_index", side_effect=OSError("rename failed")):
            with self.assertRaises(KnowledgeError):
                await self.service.update_document("alice", self.library_id, document["document_id"], {"title": "Updated"}, expected_revision=document["revision"])
            with self.assertRaises(KnowledgeError):
                await self.service.read_index("alice", self.library_id)
        self.assertIn("Updated", (await self.service.read_index("alice", self.library_id))["text"])

    async def test_failure_retry_manual_fields_and_required_review_fields(self):
        async def failed(_):
            raise RuntimeError("secret body")
        self.service.extractor = failed
        item = (await self.service.upload_imports("alice", self.library_id, [("a.md", b"# Test")]))[0]
        await self.service.process_pending()
        failed_item = await self.service.get_import("alice", self.library_id, item["import_id"])
        self.assertEqual(failed_item["status"], "failed")
        self.assertEqual(failed_item["error_code"], "extraction_failed")
        with self.assertRaises(KnowledgeError):
            await self.service.confirm_import("alice", self.library_id, item["import_id"])
        retry = await self.service.retry_import("alice", self.library_id, item["import_id"], expected_revision=failed_item["revision"])
        self.assertEqual(retry["status"], "pending")
        await self.service.process_pending()
        current = await self.service.get_import("alice", self.library_id, item["import_id"])
        manually = await self.service.edit_import("alice", self.library_id, item["import_id"], {"title": "Manual", "summary": "Owner reviewed full text"}, expected_revision=current["revision"])
        published = await self.service.confirm_import("alice", self.library_id, item["import_id"], expected_revision=manually["revision"])
        self.assertEqual(published["status"], "published")

    async def test_discard_during_extraction_cannot_resurrect_and_deletes_raw(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow(text):
            started.set()
            await finish.wait()
            return await extract(text)
        self.service.extractor = slow
        item = (await self.service.upload_imports("alice", self.library_id, [("a.md", b"# Slow")]))[0]
        processing = asyncio.create_task(self.service.process_pending())
        await started.wait()
        draft = self.repo.get_import(item["import_id"])
        result = await self.service.discard_import("alice", self.library_id, draft["import_id"], expected_revision=draft["revision"])
        finish.set()
        await processing
        self.assertEqual(result["status"], "discarded")
        self.assertEqual(self.repo.get_import(draft["import_id"])["status"], "discarded")
        with self.assertRaises(FileNotFoundError):
            self.files.read_body(draft["body_ref"])

    async def test_fixed_scope_live_permissions_and_no_new_libraries(self):
        scope = {"mode": "selected", "candidate_library_ids": [self.library_id], "selected_library_ids": [self.library_id, "lost"]}
        await self.service.create_library("alice", "new library")
        result = await self.service.list_for_scope("alice", scope)
        self.assertEqual([item["library_id"] for item in result["libraries"]], [self.library_id])
        self.assertEqual(result["unavailable_library_ids"], ["lost"])
        grant = await self.service.add_grant("alice", self.library_id, "bob")
        await self.service.revoke_grant("alice", self.library_id, grant["grant_id"])
        result = await self.service.list_for_scope("bob", scope)
        self.assertTrue(result["requires_reselection"])
        self.assertEqual(result["libraries"], [])

    async def test_delete_failure_stays_unreadable_and_restart_finishes_only_own_files(self):
        document = await self.published()
        raw = self.files.read_body(document["body_ref"])
        other_library = await self.service.create_library("alice", "Independent")
        independent = await self.published(raw, other_library["library_id"])
        replacement = await self.draft(b"# Draft replacement", replacement_document_id=document["document_id"])
        with patch.object(self.files, "delete_body", side_effect=OSError("cannot unlink")):
            deleted = await self.service.delete_document("alice", self.library_id, document["document_id"])
        self.assertTrue(deleted["cleanup_pending"])
        self.assertEqual(deleted["status"], "deleted")
        with self.assertRaises(KnowledgeError):
            await self.service.download_document("alice", self.library_id, document["document_id"])
        pending = await self.service.list_documents("alice", self.library_id)
        self.assertEqual(pending[0]["document_id"], document["document_id"])
        restarted = KnowledgeService(self.repo, self.files, extractor=extract)
        await restarted.process_pending()
        self.assertFalse(self.repo.get_document(document["document_id"])["cleanup_pending"])
        for item in (document, self.repo.get_import(replacement["import_id"])):
            with self.assertRaises(FileNotFoundError):
                self.files.read_body(item["body_ref"])
        self.assertEqual((await restarted.download_document("alice", other_library["library_id"], independent["document_id"]))["content"], raw)

    async def test_delete_library_failure_remains_visible_and_retryable(self):
        await self.published()
        await self.service.add_grant("alice", self.library_id, "bob")
        with patch.object(self.files, "delete_library_files", side_effect=OSError("busy")):
            state = await self.service.delete_library("alice", self.library_id)
        self.assertTrue(state["cleanup_pending"])
        self.assertEqual((await self.service.list_libraries("alice"))[0]["status"], "deleted")
        self.assertEqual(await self.service.list_libraries("bob"), [])
        with self.assertRaises(KnowledgeError):
            await self.service.read_index("alice", self.library_id)
        state = await self.service.delete_library("alice", self.library_id)
        self.assertFalse(state["cleanup_pending"])
        self.assertEqual(await self.service.list_libraries("alice"), [])
        await self.service.process_pending()
        self.assertEqual(self.repo.pending_work()["libraries"], [])

    async def test_replacement_target_cas_and_concurrent_confirmation_are_safe(self):
        document = await self.published()
        replacing = await self.draft(b"# Replacement", replacement_document_id=document["document_id"])
        await self.service.update_document("alice", self.library_id, document["document_id"], {"title": "Concurrent edit"}, expected_revision=document["revision"])
        with self.assertRaises(KnowledgeError) as conflict:
            await self.service.confirm_import("alice", self.library_id, replacing["import_id"])
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual((await self.service.read_document("alice", self.library_id, document["document_id"]))["sha256"], document["sha256"])
        other = await self.draft(b"# Another")
        results = await asyncio.gather(*(
            self.service.confirm_import("alice", self.library_id, other["import_id"], expected_revision=other["revision"])
            for _ in range(2)
        ))
        self.assertEqual(len({item["document_id"] for item in results}), 1)
        await self.service.process_pending()
        self.assertEqual(len(self.repo.list_documents(self.library_id)), 2)

    async def test_upload_crash_and_claim_expiry_recover_without_implicit_review(self):
        raw = b"# Uploaded before crash"
        sha = hashlib.sha256(raw).hexdigest()
        import_id = "crashed-upload"
        body_ref = self.files.build_reference(self.library_id, import_id, sha)
        item = self.repo.register_import(self.library_id, sha, "crash.md", body_ref, import_id=import_id, status="uploading")
        self.files.put_body(self.library_id, import_id, raw)
        await self.service.process_pending()
        self.assertEqual(self.repo.get_import(import_id)["status"], "uploading")
        with patch("src.capabilities.knowledge.service.time.time", return_value=item["claim_expires_at"] + 1):
            await self.service.process_pending()
        self.assertEqual(self.repo.get_import(import_id)["status"], "awaiting_review")
        self.assertEqual(self.repo.list_documents(self.library_id), [])
        another = (await self.service.upload_imports("alice", self.library_id, [("stale.md", b"# Stale processing")]))[0]
        claimed = self.repo.claim_import("dead-worker", lease_seconds=1)
        with patch("src.storage.knowledge_repository.time.time", return_value=claimed["claim_expires_at"] + 1):
            await self.service.process_pending()
        self.assertEqual(self.repo.get_import(another["import_id"])["status"], "awaiting_review")

    async def test_pointer_switch_before_process_exit_is_resumed(self):
        document = await self.published()
        draft = await self.draft(b"# New body", replacement_document_id=document["document_id"])
        self.repo.publish_import(draft["import_id"], draft["revision"])
        self.assertEqual(self.repo.get_import(draft["import_id"])["status"], "publishing")
        await KnowledgeService(self.repo, self.files, extractor=extract).process_pending()
        self.assertEqual(self.repo.get_import(draft["import_id"])["status"], "published")
        with self.assertRaises(FileNotFoundError):
            self.files.read_body(document["body_ref"])

    async def test_shutdown_releases_processing_claim_and_late_manual_edit_wins(self):
        started = asyncio.Event()
        async def hanging(text):
            started.set()
            await asyncio.Event().wait()
        self.service.extractor = hanging
        item = (await self.service.upload_imports("alice", self.library_id, [("hang.md", b"# Hanging")]))[0]
        await self.service.start()
        await started.wait()
        await self.service.shutdown()
        self.assertEqual(self.repo.get_import(item["import_id"])["status"], "pending")
        self.service.extractor = extract
        await self.service.process_pending()
        self.assertEqual(self.repo.get_import(item["import_id"])["status"], "awaiting_review")


if __name__ == "__main__":
    unittest.main()
