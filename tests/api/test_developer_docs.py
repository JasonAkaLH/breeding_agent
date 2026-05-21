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
        self.assertIn("/api/v1/conversations/chat-messages", response.text)
        self.assertIn("/api/v1/tasks/cancel", response.text)
        self.assertIn("/api/v1/tasks/interrupts/answer", response.text)
        self.assertIn("__Host-maf_session", response.text)
        self.assertIn("Authorization: Bearer", response.text)
        self.assertIn("/api/v1/auth/api-tokens", response.text)
        captcha_example = response.text.split('id="curl-post-api-v1-auth-captcha"', 1)[1].split("</code>", 1)[0]
        me_example = response.text.split('id="curl-get-api-v1-auth-me"', 1)[1].split("</code>", 1)[0]
        self.assertNotIn("-d", captcha_example)
        self.assertNotIn("conversation_id", captcha_example)
        self.assertNotIn("-d", me_example)
        self.assertNotIn("task_id", me_example)
        self.assertNotIn("/api/v1/tasks/{task_id}/cancel", response.text)
        self.assertNotIn("/api/v1/tasks/{task_id}/interrupts/{interrupt_id}/answer", response.text)
