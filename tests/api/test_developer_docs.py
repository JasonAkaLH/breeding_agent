from __future__ import annotations

import html
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
        self.assertIn("/api/v1/auth/logout", response.text)
        self.assertIn("/api/v1/auth/refresh-token", response.text)
        self.assertIn("access_token", response.text)
        self.assertIn("/api/v1/conversations/chat-messages", response.text)
        self.assertIn("metadata.deep_thinking", response.text)
        self.assertIn("Thinking / reasoning_effort 组合规则", response.text)
        self.assertIn("metadata.deep_thinking=false", response.text)
        self.assertIn("强制降为", response.text)
        self.assertIn("历史消息只持久化最终 answer content", response.text)
        self.assertIn("metadata.soft_skill_binding", response.text)
        self.assertIn("direct_skill_execution_disabled", response.text)
        self.assertIn("soft_skill_binding.decision", response.text)
        self.assertIn("main_agent.output_delta", response.text)
        self.assertIn("文件产物判定规则", response.text)
        self.assertIn("sandbox:/mnt/data", response.text)
        self.assertIn("/api/v1/artifacts/{artifact_id}/download", response.text)
        self.assertIn("/api/v1/tasks/{task_id}/events", response.text)
        self.assertIn("ConversationSummaryResponse", response.text)
        self.assertIn("username", response.text)
        self.assertIn("51888", response.text)
        self.assertIn("51999", response.text)
        self.assertIn("delete_status", response.text)
        self.assertIn("runner_id", response.text)
        self.assertIn("客户端断开后后端仍继续执行物理删除", response.text)
        self.assertIn("deleting_failed", response.text)
        self.assertIn("没有前端/用户侧自动超时承诺", response.text)
        self.assertIn("docs/runbooks/postgresql-state-platform.md", response.text)
        unescaped_html = html.unescape(response.text)
        self.assertIn('"source_path": "field-design/SKILL.md"', unescaped_html)
        self.assertNotIn('"source_path": "skill/field-design/SKILL.md"', unescaped_html)
        openapi_response = await self.client.get("/openapi.json")
        self.assertEqual(openapi_response.status_code, 200)
        for path in openapi_response.json()["paths"]:
            self.assertIn(path, response.text)
        self.assertNotIn("__Host-maf_session", response.text)
        self.assertNotIn("Set-Cookie", response.text)
        self.assertNotIn("Cookie", response.text)
        self.assertNotIn("captcha", response.text.lower())
        self.assertNotIn("password", response.text.lower())
        self.assertNotIn("account_id", response.text)
        self.assertNotIn("RegisterRequest", response.text)
        self.assertNotIn("CreateApiToken", response.text)
