from __future__ import annotations

import hashlib
from unittest.mock import patch

from tests.api.support import APITestCase


FIELDS = {"title": "育种试验", "authors": ["研究者"], "year": 2026, "doi": None,
          "keywords": ["种子"], "summary": "全文包括分组试验与产量结果。"}


class KnowledgeRoutesTest(APITestCase):
    async def test_pdf_source_content_hash_acl_and_old_draft_gates(self):
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf, unreadable_page_pdf

        library = await self._library()
        base = f"/api/v1/knowledge/libraries/{library['library_id']}"
        raw = text_pdf("FIRST source", "LAST source")
        result = await self.client.post(base + "/imports", files=[
            ("files", ("资料.PDF", raw, "text/plain")),
            ("files", ("invalid.pdf", b"not pdf", "application/pdf")),
            ("files", ("disguised.md", raw, "text/markdown")),
            ("files", ("scan.pdf", unreadable_page_pdf(), "application/pdf")),
        ])
        accepted, invalid, disguised, scan = result.json()["imports"]
        self.assertEqual(accepted["status"], "pending")
        self.assertEqual([invalid["status"], disguised["status"]], ["failed", "failed"])
        draft_url = f"{base}/imports/{accepted['import_id']}"
        source = await self.client.get(draft_url + "/source")
        self.assertEqual(source.content, raw)
        self.assertTrue(source.headers["content-type"].startswith("application/pdf"))
        self.assertEqual(source.headers["x-content-sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(source.headers["cache-control"], "no-store")
        self.assertEqual((await self.client.get(draft_url + "/content")).status_code, 409)
        await self.runtime.knowledge_service.process_pending()
        scan_url = f"{base}/imports/{scan['import_id']}"
        failed = (await self.client.get(scan_url)).json()
        self.assertFalse(failed["text_ready"])
        edited = await self.client.patch(scan_url, json={"revision": failed["revision"], "metadata": FIELDS})
        self.assertEqual(edited.status_code, 409)
        preview = await self.client.get(draft_url + "/content")
        self.assertIn("LAST source", preview.text)
        self.assertTrue(preview.headers["content-type"].startswith("text/markdown"))
        self.assertEqual(preview.headers["x-content-sha256"], hashlib.sha256(preview.content).hexdigest())
        draft = (await self.client.get(draft_url)).json()
        published = (await self.client.post(draft_url + "/confirm", json={"revision": draft["revision"]})).json()
        for suffix in ("/source", "/content"):
            self.assertEqual((await self.client.get(draft_url + suffix)).status_code, 404)
        doc_url = f"{base}/documents/{published['document_id']}"
        exported = await self.client.get(doc_url + "/content?download=true")
        self.assertEqual(exported.content, preview.content)
        self.assertTrue(exported.headers["content-disposition"].endswith(".md"))
        self.assertEqual(exported.headers["x-content-sha256"], hashlib.sha256(exported.content).hexdigest())
        other = await self._library()
        cross = doc_url.replace(library["library_id"], other["library_id"])
        self.assertEqual((await self.client.get(cross + "/source")).status_code, 404)
        await self.login("bob")
        self.assertEqual((await self.client.get(doc_url + "/source")).status_code, 403)
        await self.login("acc-1")
        grant = (await self.client.post(base + "/grants", json={"subject_type": "user", "subject_id": "bob", "role": "reader"})).json()
        await self.login("bob")
        self.assertEqual((await self.client.get(doc_url + "/source")).content, raw)
        self.assertEqual((await self.client.get(draft_url + "/source")).status_code, 403)
        await self.login("acc-1")
        await self.client.delete(base + "/grants/" + grant["grant_id"])
        await self.login("bob")
        for suffix in ("/source", "/content"):
            self.assertEqual((await self.client.get(doc_url + suffix)).status_code, 403)

    async def asyncSetUp(self):
        await super().asyncSetUp()

        async def extract(text):
            return FIELDS

        self.runtime.knowledge_service.extractor = extract

    async def _library(self):
        response = await self.client.post("/api/v1/knowledge/libraries", json={"name": "育种", "description": ""})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def _draft(self, library_id, raw=b"# trial\r\n\nBody beyond abstract.\n"):
        response = await self.client.post(
            f"/api/v1/knowledge/libraries/{library_id}/imports",
            files=[("files", ("trial.md", raw, "text/markdown"))],
        )
        self.assertEqual(response.status_code, 202, response.text)
        import_id = response.json()["imports"][0]["import_id"]
        await self.runtime.knowledge_service.process_pending()
        return (await self.client.get(f"/api/v1/knowledge/libraries/{library_id}/imports/{import_id}")).json()

    async def _publish(self, library_id, raw=b"# trial\r\n\nBody beyond abstract.\n"):
        draft = await self._draft(library_id, raw)
        response = await self.client.post(
            f"/api/v1/knowledge/libraries/{library_id}/imports/{draft['import_id']}/confirm",
            json={"revision": draft["revision"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_owner_review_download_and_metadata_conflict(self):
        library = await self._library()
        base = f"/api/v1/knowledge/libraries/{library['library_id']}"
        raw = b"\xef\xbb\xbf# trial\r\n\nExact original bytes.\n"
        draft = await self._draft(library["library_id"], raw)
        preview = await self.client.get(f"{base}/imports/{draft['import_id']}/content")
        self.assertEqual(preview.content, raw)
        self.assertEqual(preview.headers["x-content-sha256"], hashlib.sha256(preview.content).hexdigest())
        self.assertEqual((await self.client.get(base + "/documents")).json(), {"documents": []})
        self.assertNotIn(FIELDS["title"], (await self.client.get(base + "/index")).json()["text"])
        edited = await self.client.patch(
            f"{base}/imports/{draft['import_id']}",
            json={"revision": draft["revision"], "metadata": {**FIELDS, "title": "审核标题"}},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        published = await self.client.post(
            f"{base}/imports/{draft['import_id']}/confirm", json={"revision": edited.json()["revision"]},
        )
        self.assertEqual(published.status_code, 200, published.text)
        document_id = published.json()["document_id"]
        body = await self.client.get(f"{base}/documents/{document_id}/content?download=true")
        self.assertEqual(body.content, raw)
        self.assertEqual(body.headers["x-content-sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIn("attachment", body.headers["content-disposition"])
        document = (await self.client.get(base + "/documents")).json()["documents"][0]
        patch_body = {"metadata": FIELDS, "revision": document["revision"]}
        self.assertEqual((await self.client.patch(f"{base}/documents/{document_id}", json=patch_body)).status_code, 200)
        self.assertEqual((await self.client.patch(f"{base}/documents/{document_id}", json=patch_body)).status_code, 409)
        self.assertNotIn("body_ref", str(document))

    async def test_shared_reader_direct_api_write_denied_and_revoke_immediate(self):
        library = await self._library()
        library_id = library["library_id"]
        base = f"/api/v1/knowledge/libraries/{library_id}"
        document_id = (await self._publish(library_id))["document_id"]
        await self.login("bob")
        await self.login("acc-1")
        grant_body = {"subject_type": "user", "subject_id": "bob", "role": "reader"}
        first = await self.client.post(base + "/grants", json=grant_body)
        second = await self.client.post(base + "/grants", json=grant_body)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["grant_id"], second.json()["grant_id"])
        await self.login("bob")
        listed = (await self.client.get("/api/v1/knowledge/libraries")).json()["libraries"]
        self.assertEqual(listed[0]["role"], "reader")
        self.assertEqual((await self.client.get(base + "/index")).status_code, 200)
        content_url = f"{base}/documents/{document_id}/content"
        self.assertEqual((await self.client.get(content_url)).status_code, 200)
        for method, suffix, kwargs in (
            ("GET", "/imports", {}), ("GET", "/grants", {}),
            ("PATCH", "", {"json": {"name": "spoof", "description": "", "revision": 1}}),
            ("DELETE", "", {}), ("DELETE", f"/documents/{document_id}", {}),
            ("POST", "/grants", {"json": grant_body}),
            ("POST", "/imports", {"files": [("files", ("bad.md", b"# raw", "text/markdown"))]}),
        ):
            response = await self.client.request(method, base + suffix, **kwargs)
            self.assertEqual(response.status_code, 403, (method, suffix, response.text))
        await self.login("acc-1")
        self.assertEqual((await self.client.delete(base + "/grants/" + first.json()["grant_id"])).status_code, 204)
        await self.login("bob")
        self.assertEqual((await self.client.get(content_url)).status_code, 403)
        self.assertEqual((await self.client.get(base + "/index")).status_code, 403)

    async def test_grants_reject_unknown_user_team_role_and_owner_spoof(self):
        library = await self._library()
        url = f"/api/v1/knowledge/libraries/{library['library_id']}/grants"
        self.assertEqual((await self.client.post(url, json={"subject_id": "unknown-person"})).status_code, 404)
        self.assertEqual((await self.client.post(url, json={"subject_type": "team", "subject_id": "team"})).status_code, 422)
        self.assertEqual((await self.client.post(url, json={"subject_id": "acc-1", "role": "owner"})).status_code, 422)
        self.assertEqual((await self.client.post("/api/v1/knowledge/libraries", json={"name": "x", "owner": "bob"})).status_code, 422)

    async def test_batch_results_report_individual_confirmation_failure(self):
        library_id = (await self._library())["library_id"]
        draft = await self._draft(library_id)
        response = await self.client.post(
            f"/api/v1/knowledge/libraries/{library_id}/imports/confirm",
            json={"imports": [{"import_id": draft["import_id"], "revision": draft["revision"]},
                              {"import_id": "missing", "revision": 1}]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["status"] for item in response.json()["results"]], ["published", "failed"])

    async def test_upload_bound_and_authentication(self):
        library_id = (await self._library())["library_id"]
        url = f"/api/v1/knowledge/libraries/{library_id}/imports"
        with patch("src.api.routes.knowledge.DEFAULT_MAX_UPLOAD_FILE_BYTES", 4):
            response = await self.client.post(url, files=[("files", ("large.md", b"12345", "text/markdown"))])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["imports"][0]["error"], "knowledge_file_too_large")
        self.assertEqual((await self.client.get(url)).json()["imports"], [])
        await self.logout()
        self.assertEqual((await self.client.get("/api/v1/knowledge/libraries")).status_code, 401)

    async def test_batch_upload_continues_before_and_after_oversize_file(self):
        library_id = (await self._library())["library_id"]
        url = f"/api/v1/knowledge/libraries/{library_id}/imports"
        with patch("src.api.routes.knowledge.DEFAULT_MAX_UPLOAD_FILE_BYTES", 12):
            response = await self.client.post(url, files=[
                ("files", ("first.md", b"# first", "text/markdown")),
                ("files", ("large.md", b"x" * 13, "text/markdown")),
                ("files", ("last.md", b"# last", "text/markdown")),
            ])
        self.assertEqual(response.status_code, 202, response.text)
        results = response.json()["imports"]
        self.assertEqual([item["filename"] for item in results], ["first.md", "large.md", "last.md"])
        self.assertEqual(results[1]["status"], "failed")
        self.assertTrue(results[0].get("import_id"))
        self.assertTrue(results[2].get("import_id"))
        self.assertEqual(len((await self.client.get(url)).json()["imports"]), 2)
