from __future__ import annotations

import re
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
        self.assertIn("Cookie 结构", response.text)
        self.assertIn("opaque-session-id", response.text)
        self.assertIn("Max-Age=28800", response.text)
        self.assertIn("cookie-set-cookie-example", response.text)
        self.assertIn("Set-Cookie 示例", response.text)
        self.assertNotIn("manual-test", response.text)
        self.assertNotIn("手动验证当前改动", response.text)
        self.assertIn("/api/v1/conversations/{conversation_id}/messages", response.text)
        self.assertIn("/api/v1/conversations/chat-messages", response.text)
        self.assertIn("/api/v1/tasks/cancel", response.text)
        self.assertIn("/api/v1/tasks/interrupts/answer", response.text)
        self.assertIn("__Host-maf_session", response.text)
        self.assertIn("Authorization: Bearer", response.text)
        self.assertIn("/api/v1/auth/api-tokens", response.text)
        self.assertIn("参数明细", response.text)
        self.assertIn("返回参数明细", response.text)
        self.assertIn("grid-template-columns: 1fr; gap: 14px;", response.text)
        self.assertIn("conversation:read", response.text)
        self.assertIn("force_capability", response.text)
        self.assertIn("unfinished", response.text)
        self.assertIn("access_token</code></td><td><code class=\"inline-code\">string", response.text)
        self.assertIn("messages[].stream_status", response.text)
        self.assertIn("data.event_type", response.text)
        self.assertIn("preview.columns", response.text)
        self.assertIn("artifacts[].download_url", response.text)
        self.assertIn("X-Content-Type-Options", response.text)
        self.assertIn('<h4><code class="inline-code">metadata</code> 明细</h4>', response.text)
        self.assertIn("metadata.upload_ids", response.text)
        self.assertIn("metadata.main_agent_reasoning_effort", response.text)
        self.assertIn("metadata.main_agent_thinking_enabled", response.text)
        self.assertIn("metadata.auto_skill_matching_enabled", response.text)
        self.assertIn("metadata.forced_by_slash_command", response.text)
        self.assertIn("metadata.slash_command", response.text)
        self.assertIn("continued_from_pending_skill_context", response.text)
        endpoint_cards = re.findall(r'<article class="endpoint-card" id="[^"]+"', response.text)
        self.assertEqual(len(endpoint_cards), 26)
        self.assertEqual(response.text.count('class="param-section"'), 27)
        self.assertEqual(response.text.count('class="response-section"'), 26)
        self.assertEqual(response.text.count("返回参数明细"), 26)
        self.assertEqual(response.text.count('<b>请求说明</b>'), 26)
        self.assertNotIn('<b>必填参数</b>', response.text)
        self.assertNotIn('<b>请求</b>', response.text)
        captcha_example = response.text.split('id="curl-post-api-v1-auth-captcha"', 1)[1].split("</code>", 1)[0]
        me_example = response.text.split('id="curl-get-api-v1-auth-me"', 1)[1].split("</code>", 1)[0]
        self.assertNotIn("-d", captcha_example)
        self.assertNotIn("conversation_id", captcha_example)
        self.assertNotIn("-d", me_example)
        self.assertNotIn("task_id", me_example)
        self.assertNotIn("/api/v1/tasks/{task_id}/cancel", response.text)
        self.assertNotIn("/api/v1/tasks/{task_id}/interrupts/{interrupt_id}/answer", response.text)
