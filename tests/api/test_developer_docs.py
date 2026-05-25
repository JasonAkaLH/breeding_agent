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
        self.assertIn("小奥 Agent API 文档", response.text)
        self.assertIn("Authorization: Bearer", response.text)
        self.assertIn("/api/v1/auth/login", response.text)
        self.assertIn("access_token", response.text)
        self.assertIn("/api/v1/conversations/chat-messages", response.text)
        self.assertIn("/api/v1/tasks/{task_id}/events", response.text)
        self.assertIn("ConversationSummaryResponse", response.text)
        self.assertIn("username", response.text)
        self.assertNotIn("__Host-maf_session", response.text)
        self.assertNotIn("Set-Cookie", response.text)
        self.assertNotIn("captcha", response.text.lower())
        self.assertNotIn("password", response.text.lower())
        self.assertNotIn("account_id", response.text)
