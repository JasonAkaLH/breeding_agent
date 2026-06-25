from __future__ import annotations

import html
import re
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
        self.assertIn("育种助手 API 文档", response.text)
        self.assertIn("Authorization: Bearer", response.text)
        self.assertIn("/api/v1/auth/login", response.text)
        self.assertIn("/api/v1/auth/logout", response.text)
        self.assertIn("/api/v1/auth/refresh-token", response.text)
        self.assertIn("access_token", response.text)
        self.assertIn("/api/v1/conversations/chat-messages", response.text)
        self.assertIn("metadata.deep_thinking", response.text)
        self.assertIn("metadata.interrupt_id", response.text)
        self.assertIn("interrupt_resumed", response.text)
        self.assertIn("interrupt_clarification_answer", response.text)
        self.assertIn("interrupt_mixed_processed", response.text)
        self.assertIn("interrupt_schema_switched", response.text)
        self.assertIn("slot collection revision", response.text)
        self.assertIn("Thinking / reasoning_effort 组合规则", response.text)
        self.assertIn("metadata.deep_thinking=false", response.text)
        self.assertIn("reasoning_efforts", response.text)
        self.assertIn("allow_when_thinking_disabled", response.text)
        self.assertIn("validation error", response.text)
        self.assertNotIn("强制降为", response.text)
        self.assertIn("历史消息只持久化最终 answer content", response.text)
        self.assertIn("API 更新日志", response.text)
        self.assertIn("api-changelog-content", response.text)
        self.assertIn("changelog-scrollback", response.text)
        self.assertIn('aria-label="API 更新日志滚动阅读窗"', response.text)
        self.assertIn("overflow-y: auto", response.text)
        self.assertIn("max-height: min(620px, 52vh)", response.text)
        self.assertIn("api-doc/API更新日志.md", response.text)
        self.assertIn("openapi-schema-status", response.text)
        self.assertIn("data-openapi-source=\"openapi.json\"", response.text)
        self.assertIn("fetch(source, { cache: 'no-store' })", response.text)
        self.assertIn("components.schemas", response.text)
        self.assertIn("字段结构在浏览器运行时读取", response.text)
        self.assertNotIn('data-source="/api-doc/API更新日志.md"', response.text)
        self.assertNotIn('data-openapi-source="/openapi.json"', response.text)
        self.assertIn("http://127.0.0.1:51999/seedpilot/", response.text)
        self.assertIn("/seedpilot/api/", response.text)
        self.assertIn("/seedpilot/api-doc", response.text)
        self.assertIn("/assets/</code> 不再由 SeedPilot 占用", response.text)
        self.assertIn("51999/seedpilot", response.text)
        self.assertIn("仅用于内部调试", response.text)
        self.assertIn("CSV / TSV / JSON 多编码", response.text)
        self.assertIn("file_type=csv", response.text)
        self.assertIn("sheet_selection_required", response.text)
        self.assertIn("upload_sheet_selections", response.text)
        self.assertIn("clarification_answer", response.text)
        self.assertIn("assistant_message", response.text)
        self.assertIn("node.ready_to_resume", response.text)
        self.assertIn("source_encoding", response.text)
        self.assertIn("file_type=text", response.text)
        self.assertIn("normalized_content_type=text/plain", response.text)
        self.assertIn("char_count", response.text)
        self.assertIn("line_count", response.text)
        self.assertIn("size_bytes", response.text)
        self.assertIn("requires_sheet_selection", response.text)
        self.assertIn("不会消费原 open interrupt", response.text)
        self.assertIn("不会创建 message / task", response.text)
        self.assertIn("任务终态仍保持", response.text)
        self.assertIn("columns_truncated", response.text)
        self.assertIn("excel_sheets_truncated", response.text)
        self.assertIn("metadata.soft_skill_binding", response.text)
        self.assertIn("direct_skill_execution_disabled", response.text)
        self.assertIn("skill.contract.yaml", response.text)
        self.assertIn("schemas/*.input.yaml", response.text)
        self.assertIn("SkillResourceService", response.text)
        self.assertIn("schema_version=2", response.text)
        self.assertIn("selected_schema_id", response.text)
        self.assertIn("selected_entrypoint", response.text)
        self.assertIn("resource_hints", response.text)
        self.assertIn("skill.resource_read", response.text)
        self.assertIn("skill.output_contract_validated", response.text)
        self.assertIn("主代理不能读取", response.text)
        self.assertIn("scripts/", response.text)
        self.assertIn("runtime/", response.text)
        self.assertIn("schemas/", response.text)
        self.assertIn("config.yaml", response.text)
        self.assertIn("soft_skill_binding.decision", response.text)
        self.assertIn("main_agent.output_delta", response.text)
        self.assertIn("文件产物判定规则", response.text)
        self.assertIn("sandbox:/mnt/data", response.text)
        self.assertIn("/api/v1/artifacts/{artifact_id}/download", response.text)
        self.assertIn("/api/v1/tasks/{task_id}/events", response.text)
        endpoint_summaries = re.findall(r'<div class="endpoint-summary"><div><h3>.*?</h3><p>(.*?)</p></div></div>', response.text, re.S)
        self.assertGreaterEqual(len(endpoint_summaries), 19)
        for summary in endpoint_summaries:
            self.assertIn("典型时机：", summary)
        external_explanations = re.findall(r'<section class="external-explanation"><h4>外部调用方说明</h4><ul>(.*?)</ul></section>', response.text, re.S)
        self.assertEqual(len(external_explanations), len(endpoint_summaries))
        self.assertIn("给外部系统的完整任务链路", response.text)
        self.assertIn("以 SSE 为主账本", response.text)
        self.assertIn("只有当 SSE 中收到", response.text)
        self.assertIn("不能替代 SSE 触发 interrupt", response.text)
        self.assertIn("graph 只能说明“现在看起来是什么状态”", response.text)
        self.assertIn("所有 interrupt 回答统一通过 chat-messages 提交", response.text)
        self.assertIn("开放性追问", response.text)
        self.assertIn("metadata.upload_sheet_selections", response.text)
        self.assertIn("SSE event_type 枚举", response.text)
        event_enum_section = response.text[
            response.text.index("SSE event_type 枚举"):response.text.index("流式持久化规则")
        ]
        for expected_label in (
            "task.accepted</code>（任务接受）",
            "task.graph_created</code>（任务图创建完成）",
            "auth.invalidated</code>（认证失效）",
            "mcp.tool_call_completed</code>（MCP 工具调用完成）",
            "artifact.download_gone</code>（Artifact 下载已失效）",
        ):
            self.assertIn(expected_label, event_enum_section)
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
            "task.interrupt_clarification_answered",
            "node.started",
            "node.completed",
            "node.failed",
            "node.waiting_for_input",
            "node.cancelled",
            "node.blocked_by_cancellation",
            "node.orphaned",
            "node.ready_to_resume",
            "node.resuming",
            "main_agent.output_delta",
            "main_agent.reasoning_delta",
            "planner.reasoning_delta",
            "interrupt.reasoning_delta",
            "memory.reasoning_delta",
            "soft_skill.reasoning_delta",
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
            "skill.resource_read",
            "skill.output_contract_validated",
            "soft_skill_binding.decision",
            "soft_skill_binding.llm_failed",
            "task.late_result_discarded",
            "task.replanned",
            "workflow.plan_built",
        ):
            self.assertIn(event_type, response.text)
            self.assertRegex(
                event_enum_section,
                rf'<code class="inline-code">{re.escape(event_type)}</code>（[^）]+）',
            )
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
        self.assertIn('"stream_status": "complete"', unescaped_html)
        self.assertNotIn('"stream_status": "completed"', unescaped_html)
        self.assertNotIn('"source_path": "skill/field-design/SKILL.md"', unescaped_html)
        openapi_response = await self.client.get("/openapi.json")
        self.assertEqual(openapi_response.status_code, 200)
        openapi = openapi_response.json()
        self.assertNotIn("/api/v1/tasks/interrupts/answer", openapi["paths"])
        self.assertNotIn("/api/v1/tasks/interrupts/answer", response.text)
        self.assertNotIn("AnswerInterruptRequest", response.text)
        self.assertNotIn("AnswerInterruptResponse", response.text)
        for path in openapi["paths"]:
            self.assertIn(path, response.text)
        message_accepted_schema = openapi["components"]["schemas"]["MessageAcceptedResponse"]
        self.assertIn("action", message_accepted_schema["properties"])
        self.assertIn("interrupt_id", message_accepted_schema["properties"])
        self.assertIn("assistant_message", message_accepted_schema["properties"])
        self.assertIn("answer_payload", message_accepted_schema["properties"])
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
        self.assertIn("2026-06-09", changelog_response.text)
        self.assertIn("_slot_collection_ref", changelog_response.text)
        self.assertIn("client_request_id", changelog_response.text)
        self.assertIn("clarification_answer", changelog_response.text)
        self.assertIn("metadata.interrupt_id", changelog_response.text)
        self.assertIn("MessageAcceptedResponse.action", changelog_response.text)
        self.assertIn("interrupt_resumed", changelog_response.text)
        self.assertIn("interrupt_mixed_processed", changelog_response.text)
        self.assertIn("interrupt_schema_switched", changelog_response.text)
        self.assertIn("collection_id + revision", changelog_response.text)
        self.assertIn("will_resume=true", changelog_response.text)
        self.assertIn("interrupt_clarification_answer", changelog_response.text)
        self.assertIn("assistant_message", changelog_response.text)
        self.assertIn("History Recall", changelog_response.text)
        self.assertIn("model_edition", changelog_response.text)
        self.assertIn("messages[].artifacts", changelog_response.text)
        self.assertIn("file_type=vcf", changelog_response.text)
        self.assertIn("file_type=text", changelog_response.text)
        self.assertIn("char_count", changelog_response.text)
        self.assertIn("line_count", changelog_response.text)
        self.assertNotIn("/api/v1/tasks/interrupts/answer", changelog_response.text)
        self.assertNotIn("AnswerInterrupt", changelog_response.text)
        self.assertIn("2026-06-05", changelog_response.text)
        self.assertIn("v2-only Skill Contract", changelog_response.text)
        self.assertIn("skill.contract.yaml", changelog_response.text)
        self.assertIn("_slot_collection.schema_version=2", changelog_response.text)
        self.assertIn("SkillResourceService", changelog_response.text)
        self.assertIn("skill.resource_read", changelog_response.text)
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
