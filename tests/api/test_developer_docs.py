from __future__ import annotations

from pathlib import Path

from tests.api.support import APITestCase


class DeveloperDocsAPITest(APITestCase):
    async def test_api_doc_endpoint_serves_static_html_without_authentication(self) -> None:
        docs_file = Path("docs/api/api-doc.html")
        self.assertTrue(docs_file.is_file())

        await self.logout()
        response = await self.client.get("/api-doc")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("API文档", response.text)
        self.assertIn("接口控制台", response.text)
        self.assertIn("/api/v1/conversations/{conversation_id}/messages", response.text)
