from __future__ import annotations

import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, text

from src.storage.knowledge_files import KnowledgeFileStore
from src.storage.knowledge_repository import KnowledgeConflict, KnowledgeRepository
from src.storage.sqlalchemy_base import SQLiteBase
from src.storage.sqlite.bootstrap import bootstrap_sqlite_database
from src.storage.sqlite.session import (
    create_sqlite_engine,
    create_sqlite_session_factory,
)


class KnowledgeRepositoryContract:
    def test_pdf_claim_pair_publication_and_cleanup_contract(self):
        library = self.library()
        sha = hashlib.sha256(b"pdf-source").hexdigest()
        ref = KnowledgeFileStore.build_reference(library["library_id"], uuid4().hex, sha, "pdf")
        row = self.repo.register_import(library["library_id"], sha, "paper.pdf", ref)
        row = self.repo.update_import(row["import_id"], row["revision"], title="Paper", summary="Full findings", status="awaiting_review")
        with self.assertRaises(KnowledgeConflict):
            self.repo.publish_import(row["import_id"], row["revision"])
        self.repo.update_import(row["import_id"], row["revision"], status="pending")
        claim = self.repo.claim_import("worker")
        parsed_sha = hashlib.sha256(b"parsed text").hexdigest()
        parsed_ref = KnowledgeFileStore.build_reference(library["library_id"], uuid4().hex, parsed_sha)
        with self.assertRaises(KnowledgeConflict):
            self.repo.register_parsed_body(row["import_id"], claim["revision"], "wrong", sha, parsed_ref, parsed_sha)
        registered = self.repo.register_parsed_body(row["import_id"], claim["revision"], claim["claim_token"], sha, parsed_ref, parsed_sha)
        with self.assertRaises(KnowledgeConflict):
            self.repo.update_import(row["import_id"], claim["revision"], claim_token=claim["claim_token"], status="awaiting_review")
        reviewed = self.repo.update_import(row["import_id"], registered["revision"], claim_token=claim["claim_token"], status="awaiting_review")
        document = self.publish(reviewed)
        self.assertEqual(document["parsed_body_ref"], parsed_ref)
        deleted = self.repo.delete_document(document["document_id"])
        self.assertEqual(set(deleted["cleanup_refs"]), {ref, parsed_ref})

    def library(self, owner="alice"):
        return self.repo.create_library(owner, "研究", "说明")

    def draft(self, library, raw=b"# Full text\r\nbody", **kwargs):
        digest = hashlib.sha256(raw).hexdigest()
        imported = self.repo.register_import(
            library["library_id"],
            digest,
            "paper.md",
            KnowledgeFileStore.build_reference(
                library["library_id"], uuid4().hex, digest
            ),
            **kwargs,
        )
        if "duplicate_type" not in imported:
            imported = self.repo.update_import(
                imported["import_id"],
                imported["revision"],
                title="Paper",
                summary="Findings from full text",
                status="awaiting_review",
            )
        return imported

    def publish(self, imported):
        document = self.repo.publish_import(imported["import_id"], imported["revision"])
        library = self.repo.get_library(document["library_id"])
        self.repo.set_index_published(library["library_id"], library["index_revision"])
        self.repo.finish_document_cleanup(document["document_id"], document["revision"])
        self.repo.complete_publication(imported["import_id"])
        return self.repo.get_document(document["document_id"])

    def test_raw_sha_cross_table_dedup_and_reimport_after_delete(self):
        library = self.library()
        imported = self.draft(library)
        duplicate = self.draft(library)
        self.assertEqual(
            (duplicate["duplicate_type"], duplicate["import_id"]),
            ("import", imported["import_id"]),
        )
        document = self.publish(imported)
        duplicate = self.draft(library)
        self.assertEqual(
            (duplicate["duplicate_type"], duplicate["document_id"]),
            ("document", document["document_id"]),
        )
        self.repo.delete_document(document["document_id"], document["revision"])
        again = self.draft(library)
        self.assertNotIn("duplicate_type", again)
        self.assertNotEqual(imported["import_id"], again["import_id"])
        self.assertNotEqual(
            self.draft(library, b"# Full text\nbody")["import_id"], again["import_id"]
        )
        other = self.draft(self.library(), b"# Full text\r\nbody")
        self.assertNotIn("duplicate_type", other)

    def test_concurrent_register_same_library_one_draft(self):
        library = self.library()
        sha = hashlib.sha256(b"one").hexdigest()

        def upload(_):
            return self.repo.register_import(
                library["library_id"],
                sha,
                "one.md",
                KnowledgeFileStore.build_reference(
                    library["library_id"], uuid4().hex, sha
                ),
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(upload, range(4)))
        self.assertEqual(len({row["import_id"] for row in results}), 1)
        self.assertEqual(len(self.repo.list_imports(library["library_id"])), 1)

    def test_publication_review_gate_and_replacement_cas_cleanup(self):
        library = self.library()
        sha = hashlib.sha256(b"raw").hexdigest()
        row = self.repo.register_import(
            library["library_id"],
            sha,
            "a.md",
            KnowledgeFileStore.build_reference(library["library_id"], uuid4().hex, sha),
        )
        with self.assertRaises(KnowledgeConflict):
            self.repo.publish_import(row["import_id"])
        with self.assertRaises(ValueError):
            self.repo.publish_import(
                self.repo.update_import(row["import_id"], status="awaiting_review")[
                    "import_id"
                ]
            )
        self.repo.discard_import(row["import_id"])
        document = self.publish(self.draft(library))
        replacing = self.draft(
            library,
            b"replacement",
            replacement_document_id=document["document_id"],
            target_revision=document["revision"],
        )
        self.repo.update_document(
            document["document_id"], document["revision"], title="Changed"
        )
        with self.assertRaises(KnowledgeConflict):
            self.repo.publish_import(replacing["import_id"])
        self.repo.discard_import(replacing["import_id"])
        current = self.repo.get_document(document["document_id"])
        replacing = self.draft(
            library,
            b"replacement",
            replacement_document_id=current["document_id"],
            target_revision=current["revision"],
        )
        changed = self.repo.publish_import(replacing["import_id"])
        self.assertEqual(changed["document_id"], document["document_id"])
        self.assertEqual(changed["cleanup_refs"], [document["body_ref"]])
        self.assertEqual(self.repo.list_documents(library["library_id"]), [])
        with self.assertRaises(KnowledgeConflict):
            self.repo.complete_publication(replacing["import_id"])
        new_library = self.repo.get_library(library["library_id"])
        self.repo.set_index_published(
            library["library_id"], new_library["index_revision"]
        )
        with self.assertRaises(KnowledgeConflict):
            self.repo.complete_publication(replacing["import_id"])
        self.repo.finish_document_cleanup(changed["document_id"], changed["revision"])
        self.repo.complete_publication(replacing["import_id"])
        self.assertEqual(
            self.repo.list_documents(library["library_id"])[0]["sha256"],
            replacing["sha256"],
        )

    def test_claim_cas_rejects_late_extraction_and_library_delete(self):
        library = self.library()
        sha = hashlib.sha256(b"source").hexdigest()
        imported = self.repo.register_import(
            library["library_id"],
            sha,
            "source.md",
            KnowledgeFileStore.build_reference(library["library_id"], uuid4().hex, sha),
        )
        claimed = self.repo.claim_import("one")
        self.assertEqual(claimed["import_id"], imported["import_id"])
        self.assertIsNone(self.repo.claim_import("two"))
        self.assertFalse(self.repo.renew_import_claim(imported["import_id"], "wrong"))
        self.assertTrue(
            self.repo.renew_import_claim(
                imported["import_id"], claimed["claim_token"], 120
            )
        )
        self.assertEqual(
            self.repo.get_import(imported["import_id"])["revision"], claimed["revision"]
        )
        self.repo.discard_import(imported["import_id"], claimed["revision"])
        with self.assertRaises(KnowledgeConflict):
            self.repo.update_import(
                imported["import_id"],
                claimed["revision"],
                claim_token=claimed["claim_token"],
                title="late",
                status="awaiting_review",
            )
        pending = self.repo.pending_work()
        self.assertTrue(
            any(
                row["import_id"] == imported["import_id"] and row["cleanup_pending"]
                for row in pending["imports"]
            )
        )
        self.repo.delete_library(library["library_id"])
        self.assertIsNone(self.repo.claim_import("three"))

    def test_year_integer_and_library_cleanup_leave_no_permanent_pending_work(self):
        library = self.library()
        imported = self.draft(library)
        imported = self.repo.update_import(
            imported["import_id"], imported["revision"], year=2026
        )
        self.assertIsInstance(imported["year"], int)
        document = self.publish(imported)
        self.assertEqual(document["year"], 2026)
        self.assertIsInstance(document["year"], int)
        deleted = self.repo.delete_library(library["library_id"])
        self.repo.finish_library_cleanup(library["library_id"], deleted["revision"])
        pending = self.repo.pending_work()
        for values in pending.values():
            self.assertFalse(
                any(value["library_id"] == library["library_id"] for value in values)
            )

    def test_index_revision_cas_rejects_old_projection(self):
        library = self.library()
        snapshot = self.repo.index_snapshot(library["library_id"])
        self.repo.update_library(
            library["library_id"], library["revision"], description="new"
        )
        self.assertFalse(
            self.repo.set_index_published(
                library["library_id"], snapshot["library"]["index_revision"]
            )
        )
        latest = self.repo.index_snapshot(library["library_id"])
        self.assertTrue(
            self.repo.set_index_published(
                library["library_id"], latest["library"]["index_revision"]
            )
        )

    def test_selection_scope_immutable_owner_bound_and_delete_independent(self):
        library, shared = self.library(), self.library("bob")
        self.repo.grant(shared["library_id"], "alice")
        self.repo.grant(shared["library_id"], "alice")
        self.assertEqual(len(self.repo.list_grants(shared["library_id"])), 1)
        cid = uuid4().hex
        self.assertEqual(self.repo.get_selection("alice", cid)["revision"], 0)
        selected = self.repo.save_selection(
            "alice", cid, "selected", [library["library_id"]], 0
        )
        frozen = self.repo.freeze_scope("alice", cid, uuid4().hex)
        self.repo.save_selection("alice", cid, "auto", [], selected["revision"])
        self.assertEqual(
            self.repo.freeze_scope("alice", cid, frozen["root_message_id"]), frozen
        )
        self.assertEqual(frozen["candidate_library_ids"], [library["library_id"]])
        with self.assertRaises(KnowledgeConflict):
            self.repo.save_selection("bob", cid, "auto", [])
        with self.assertRaises(KnowledgeConflict):
            self.repo.get_scope(frozen["root_message_id"], "bob", cid)
        with self.assertRaises(ValueError):
            self.repo.save_selection("alice", cid, "selected", [])
        self.repo.delete_conversation_context(cid)
        self.assertIsNone(self.repo.get_scope(frozen["root_message_id"]))
        self.assertIsNotNone(self.repo.get_library(library["library_id"]))
        self.repo.revoke(shared["library_id"], "alice")
        with self.assertRaises(KnowledgeConflict):
            self.repo.save_selection(
                "alice", uuid4().hex, "selected", [shared["library_id"]]
            )

    def test_scope_same_message_concurrent_snapshot_has_single_winner(self):
        self.library()
        cid, message = uuid4().hex, uuid4().hex
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: self.repo.freeze_scope("alice", cid, message), range(4)
                )
            )
        self.assertTrue(all(item == results[0] for item in results))


class KnowledgeRepositoryTest(KnowledgeRepositoryContract, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.directory.name) / "state.db")
        bootstrap_sqlite_database(self.engine)
        self.repo = KnowledgeRepository(create_sqlite_session_factory(self.engine))

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def test_additive_bootstrap_and_existing_business_data(self):
        library = self.library()
        draft = self.draft(library)
        document = self.publish(self.draft(library, b"published old Markdown"))
        self.repo.grant(library["library_id"], "bob")
        self.repo.save_selection("alice", "before", "auto", [])
        # This engine is the test's disposable SQLite file, never an app database.
        with self.engine.begin() as connection:
            for table in ("knowledge_import", "knowledge_document"):
                for column in ("parsed_body_ref", "parsed_body_sha256"):
                    connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        bootstrap_sqlite_database(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.assertIsNone(self.repo.get_import(draft["import_id"])["parsed_body_ref"])
        self.assertEqual(self.repo.get_document(document["document_id"])["sha256"], document["sha256"])
        self.assertEqual(self.repo.list_grants(library["library_id"])[0]["subject_id"], "bob")
        self.assertEqual(self.repo.get_selection("alice", "before")["mode"], "auto")
        self.assertEqual(
            len(inspect(self.engine).get_table_names()), len(SQLiteBase.metadata.tables)
        )
