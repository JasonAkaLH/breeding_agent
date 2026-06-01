from __future__ import annotations

import html
from pathlib import Path

from tests.api.support import APITestCase


class DeveloperDocsAPITest(APITestCase):
    async def test_api_doc_endpoint_serves_static_html_without_authentication(self) -> None:
        docs_file = Path("docs/api/api-doc.html")
        changelog_file = Path("docs/api/API更新日志.md")
        self.assertTrue(docs_file.is_file())
        self.assertTrue(changelog_file.is_file())

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
        self.assertIn("API 更新日志", response.text)
        self.assertIn("api-changelog-content", response.text)
        self.assertIn("/api-doc/API更新日志.md", response.text)
        self.assertIn("CSV / JSON 多编码", response.text)
        self.assertIn("sheet_selection_required", response.text)
        self.assertIn("upload_sheet_selections", response.text)
        self.assertIn("source_encoding", response.text)
        self.assertIn("requires_sheet_selection", response.text)
        self.assertIn("不会消费原 open interrupt", response.text)
        self.assertIn("不会创建 message / task", response.text)
        self.assertIn("任务终态仍保持", response.text)
        self.assertIn("columns_truncated", response.text)
        self.assertIn("excel_sheets_truncated", response.text)
        self.assertIn("metadata.soft_skill_binding", response.text)
        self.assertIn("direct_skill_execution_disabled", response.text)
        self.assertIn("soft_skill_binding.decision", response.text)
        self.assertIn("main_agent.output_delta", response.text)
        self.assertIn("文件产物判定规则", response.text)
        self.assertIn("sandbox:/mnt/data", response.text)
        self.assertIn("/api/v1/artifacts/{artifact_id}/download", response.text)
        self.assertIn("/api/v1/tasks/{task_id}/events", response.text)
        self.assertIn("SSE event_type 枚举", response.text)
        for event_type in (
            "auth.invalidated",
            "task.accepted",
            "task.graph_created",
            "task.graph_updated",
            "task.replan_started",
            "task.replan_rejected",
            "task.replan_available",
            "task.completed",
            "task.failed",
            "task.cancellation_requested",
            "task.cancelled",
            "task.interrupt_answered",
            "node.started",
            "node.completed",
            "node.failed",
            "node.waiting_for_input",
            "node.cancelled",
            "node.blocked_by_cancellation",
            "main_agent.output_delta",
            "main_agent.reasoning_delta",
            "main_agent.output_final",
            "skill.progress",
            "mcp.long_task_started",
            "mcp.long_task_progress",
            "mcp.long_task_status",
            "mcp.long_task_completed",
            "mcp.long_task_failed",
            "mcp.long_task_cancel_requested",
            "mcp.long_task_cancelled",
            "artifact.download_denied",
            "artifact.download_gone",
            "artifact.downloaded",
            "assistant_history_sync.failed",
            "conversation.memory_built",
            "conversation.memory_fallback",
            "main_agent.llm_call",
            "main_agent.llm_stream_failed",
            "main_agent.prompt_envelope_failed",
            "main_agent.prompt_envelope_rendered",
            "main_agent.prompt_profile_rendered",
            "main_agent.stream_cancelled",
            "mcp.tool_call_blocked",
            "mcp.tool_call_started",
            "mcp.tool_call_failed",
            "mcp.tool_call_completed",
            "pending_skill_context.superseded",
            "pending_skill_context.consumed",
            "pending_skill_context.created",
            "skill.bundle_missing",
            "skill.entrypoint_started",
            "skill.entrypoint_failed",
            "skill.entrypoint_completed",
            "skill.execution_started",
            "skill.execution_failed",
            "skill.execution_completed",
            "skill.execution_interrupted",
            "skill.forced_missing",
            "skill.forced_selected",
            "skill.input_resolution_prompt_profile",
            "skill.input_resolution_diagnostic",
            "skill.input_resolved",
            "skill.input_missing",
            "skill.match_fallback",
            "skill.match_suppressed",
            "skill.matched",
            "skill.output_error",
            "skill.output_file_rejected",
            "skill.output_file_collected",
            "skill.script_started",
            "skill.script_failed",
            "skill.script_completed",
            "skill.service_denied",
            "skill.service_bound",
            "soft_skill_binding.decision",
            "soft_skill_binding.llm_failed",
            "task.late_result_discarded",
            "task.replanned",
            "workflow.plan_built",
        ):
            self.assertIn(event_type, response.text)
        self.assertNotIn("task.updated", response.text)
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

        changelog_response = await self.client.get("/api-doc/API更新日志.md")
        self.assertEqual(changelog_response.status_code, 200)
        self.assertIn("text/markdown", changelog_response.headers["content-type"])
        self.assertIn("# API 更新日志", changelog_response.text)
        self.assertIn("2026-06-01", changelog_response.text)
        self.assertIn("sheet_selection_required", changelog_response.text)
        self.assertIn("upload_sheet_selections", changelog_response.text)
        self.assertIn("不会创建 message / task", changelog_response.text)
        self.assertIn("保持原 interrupt open", changelog_response.text)
        self.assertIn("任务终态仍保持 `cancelled`", changelog_response.text)
        self.assertIn("2026-05-28 至 2026-05-29", changelog_response.text)
        self.assertIn("direct_skill_execution_disabled", changelog_response.text)
        alias_response = await self.client.get("/api-doc/api-changelog.md")
        self.assertEqual(alias_response.status_code, 200)
        self.assertEqual(alias_response.text, changelog_response.text)
