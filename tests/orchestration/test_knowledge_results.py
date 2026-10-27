from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.capabilities.knowledge.executor import KnowledgeExecutor
from src.capabilities.knowledge.registration import KNOWLEDGE_CAPABILITY_DESCRIPTORS
from src.core.contracts import CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import NodeStatus, TaskStatus
from src.core.models import Task, TaskNode
from src.orchestration.agent_loop.capability_invoker import AgentCapabilityInvoker
from src.orchestration.agent_loop.compaction import AgentCompactionService
from src.orchestration.agent_loop.context import AgentContextBuilder, AgentContextRules
from src.orchestration.agent_loop.context_budget import AgentContextBudget
from src.orchestration.agent_loop.context_preflight import AgentContextCandidateBuilder, AgentContextPreflightDecision
from src.orchestration.agent_loop.knowledge_results import KnowledgeResultResolver
from src.orchestration.agent_loop.models import (
    AgentItem, AgentItemKind, AgentItemState, AgentModelBinding, AgentRun, AgentRunStatus, AgentToolCall,
)
from src.orchestration.agent_loop.result_projection import AgentCallResultProjector
from src.orchestration.agent_loop.tool_catalog import (
    AgentToolCatalog, AgentToolCatalogBuilder, CapabilityVisibilityContext, default_agent_invocation_policy,
)
from src.orchestration.registry import CapabilityRegistry


SCOPE = {"actor": "alice", "conversation_id": "conv-1", "root_message_id": "message-1",
         "mode": "selected", "library_ids": ["lib-1"], "candidate_library_ids": ["lib-1"]}
OWNER = hashlib.sha256(b"agent-owner-scope-v1\0alice").hexdigest()
METADATA = {"knowledge_scope": SCOPE, "authenticated_owner_scope": OWNER,
            "agent_run_id": "run-1", "agent_model_edition": "edition-a"}


class _Denied(Exception):
    status_code = 403


class _Service:
    def __init__(self, text: str = "唯一结果：增产 17%。\r\n第二段。") -> None:
        self.text = text
        self.allowed = True
        self.reads = 0
        self.revision = 1

    async def list_for_scope(self, actor, scope):
        return {"libraries": [{"library_id": "lib-1", "description": "育种研究"}] if self.allowed else [],
                "unavailable_library_ids": [] if self.allowed else ["lib-1"],
                "requires_reselection": not self.allowed, "mode": scope["mode"]}

    async def read_index(self, actor, library_id):
        if not self.allowed:
            raise _Denied()
        return {"text": "# 文献索引\n文献一\n", "index_revision": self.revision}

    async def read_document(self, actor, library_id, document_id):
        self.reads += 1
        if not self.allowed:
            raise _Denied()
        return {"text": self.text, "sha256": hashlib.sha256(self.text.encode()).hexdigest(),
                "version": getattr(self, "version", hashlib.sha256(self.text.encode()).hexdigest()),
                "metadata": {"title": "文献一", "authors": ["作者甲"], "year": 2026, "doi": None}}


def _request(payload=None, **metadata):
    return CapabilityExecutionRequest("knowledge.read_document", "conv-1", "task-1", "node-1",
                                      payload or {"library_id": "lib-1", "document_id": "doc-1"},
                                      metadata={**METADATA, **metadata})


def _item(name, sequence, kind, payload, **kwargs):
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return AgentItem(name, "run-1", "task-1", sequence, kind, kwargs.pop("state", AgentItemState.COMMITTED),
                     text, hashlib.sha256(text.encode()).hexdigest(), **kwargs)


def _items(result, *, window=20_000):
    return (
        _item("user", 1, AgentItemKind.USER_MESSAGE,
              {"text": "实际增产是多少？", "context_budget": AgentContextBudget.from_model_context_window(window).to_payload()}),
        _item("assistant", 2, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""}),
        _item("call", 3, AgentItemKind.TOOL_CALL,
              {"capability_id": "knowledge.read_document", "call_id": "provider-call",
               "provider_safe_name": "knowledge_read_document", "node_id": "node-1",
               "arguments_json": '{"document_id":"doc-1","library_id":"lib-1"}'}, parent_item_id="assistant"),
        _item("result", 4, AgentItemKind.TOOL_RESULT,
              {"outcome": "completed", "safe_error_code": None, "artifact_refs": [], "safe_result": result},
              source_call_item_id="call"),
    )


def _run():
    return AgentRun("run-1", "task-1", "conv-1", AgentRunStatus.RUNNING, AgentModelBinding("edition-a"),
                    next_item_sequence=5, active_sample_item_id="assistant", revision=4)


def _catalog():
    registry = CapabilityRegistry()
    for descriptor in KNOWLEDGE_CAPABILITY_DESCRIPTORS:
        registry.register(descriptor, invocation_policy=default_agent_invocation_policy(descriptor))
    return AgentToolCatalogBuilder(registry).build(CapabilityVisibilityContext(OWNER, knowledge_scope=SCOPE))


class KnowledgeResultTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_progress_survives_compaction_and_revalidates_current_source(self):
        service = _Service("evidence " * 1000)
        executor = KnowledgeExecutor(service, token_counter=lambda text, _edition: len(text) // 4)
        first = await executor.execute(_request(knowledge_result_token_budget=600))
        ref = first.output_payload["reference"]
        first_items = _items(first.output_payload)
        cursor = ref["next_cursor"]
        last = await executor.execute(_request({"library_id": "lib-1", "document_id": "doc-1", "cursor": cursor}))
        last_items = _items(last.output_payload)
        call_payload = json.loads(last_items[2].payload_json)
        call_payload["arguments_json"] = json.dumps({"library_id": "lib-1", "document_id": "doc-1", "cursor": cursor})
        more = (replace(last_items[2], item_id="call2", sequence=6, payload_json=json.dumps(call_payload)),
                replace(last_items[3], item_id="result2", sequence=7, source_call_item_id="call2"))
        resolver = KnowledgeResultResolver(service=service, scope_loader=lambda _run: SCOPE)
        run = replace(_run(), compacted_through_sequence=4, next_item_sequence=8)
        sources = await resolver.answer_sources(run=run, items=(*first_items, *more))
        self.assertTrue(sources[0]["complete"])
        self.assertEqual(sources[0]["read_characters"], len(service.text))
        self.assertNotIn(service.text[:100], json.dumps(sources))
        partial = await resolver.answer_sources(run=run, items=first_items)
        self.assertFalse(partial[0]["complete"])
        self.assertEqual(partial[0]["next_cursor"], cursor)
        service.version = "e" * 64
        changed = await resolver.answer_sources(run=run, items=(*first_items, *more))
        self.assertEqual(changed[0]["status"], "changed")
        service.allowed = False
        denied = await resolver.answer_sources(run=run, items=(*first_items, *more))
        self.assertEqual(denied[0]["status"], "forbidden")

    async def test_pdf_source_only_version_change_invalidates_cursor_and_reference(self):
        service = _Service("PDF parsed evidence " * 1000)
        service.version = "a" * 64
        executor = KnowledgeExecutor(service, token_counter=lambda text, _edition: len(text) // 4)
        first = await executor.execute(_request(knowledge_result_token_budget=600))
        self.assertIsNone(first.error)
        service.version = "b" * 64
        changed = await executor.execute(_request({"library_id": "lib-1", "document_id": "doc-1", "cursor": first.output_payload["reference"]["next_cursor"]}))
        self.assertEqual(changed.output_payload["status"], "changed")
        resolver = KnowledgeResultResolver(service=service, scope_loader=lambda _run: SCOPE)
        items = _items(first.output_payload)
        view = await resolver.resolve_tool_result(run=_run(), call_item=items[2], result_item=items[3], durable_payload=json.loads(items[3].payload_json))
        self.assertEqual(view["safe_result"]["status"], "changed")

    async def test_utf8_pages_are_contiguous_and_durable_results_contain_no_body(self):
        service = _Service("\ufeff" + "稻🌾增产 17%。\r\n" * 500)
        executor = KnowledgeExecutor(service, token_counter=lambda text, _edition: len(text) // 4)
        resolver = KnowledgeResultResolver(service=service, scope_loader=lambda _run: SCOPE)
        cursor = None
        pieces = []
        while True:
            payload = {"library_id": "lib-1", "document_id": "doc-1"}
            if cursor:
                payload["cursor"] = cursor
            execution = await executor.execute(_request(payload, knowledge_result_token_budget=600))
            self.assertIsNone(execution.error)
            self.assertNotIn("稻🌾", json.dumps(execution.output_payload, ensure_ascii=False))
            items = _items(execution.output_payload)
            items = (*items[:2], replace(items[2], payload_json=json.dumps({**json.loads(items[2].payload_json),
                     "arguments_json": json.dumps(payload)})), items[3])
            view = await resolver.resolve_tool_result(run=_run(), call_item=items[2], result_item=items[3],
                                                      durable_payload=json.loads(items[3].payload_json))
            pieces.append(view["safe_result"]["text"])
            ref = execution.output_payload["reference"]
            self.assertEqual(ref["start"], sum(map(len, pieces[:-1])))
            cursor = ref["next_cursor"]
            if cursor is None:
                self.assertTrue(view["safe_result"]["complete"])
                break
            self.assertLess(len(pieces), 100)
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), service.text)

    async def test_new_reads_revalidate_and_old_cursors_do_not_mix_versions(self):
        service = _Service("正文🌾" * 1000)
        executor = KnowledgeExecutor(service, token_counter=lambda text, _edition: len(text) // 4)
        first = await executor.execute(_request(knowledge_result_token_budget=600))
        service.text = "新正文🌾" * 1000
        changed = await executor.execute(_request({"library_id": "lib-1", "document_id": "doc-1",
                                                   "cursor": first.output_payload["reference"]["next_cursor"]}))
        self.assertEqual(changed.output_payload["status"], "changed")
        new = await executor.execute(_request())
        self.assertNotEqual(first.output_payload["reference"]["version"], new.output_payload["reference"]["version"])
        service.allowed = False
        denied = await executor.execute(_request())
        self.assertEqual(denied.output_payload["status"], "forbidden")
        self.assertEqual(service.reads, 4)

    async def test_scope_and_identity_cannot_be_supplied_by_model(self):
        service = _Service()
        executor = KnowledgeExecutor(service, token_counter=lambda text, _edition: len(text) // 4)
        denied = await executor.execute(_request({"library_id": "other", "document_id": "doc-1"}))
        self.assertEqual(denied.output_payload["status"], "unavailable")
        forged = await executor.execute(_request({"library_id": "lib-1", "document_id": "doc-1", "actor": "alice"}))
        self.assertIsNotNone(forged.error)
        forged_owner = await executor.execute(_request(authenticated_owner_scope="wrong"))
        self.assertIsNotNone(forged_owner.error)
        self.assertEqual(service.reads, 0)

    async def test_projection_accepts_only_closed_reference_and_never_stages(self):
        service = _Service()
        result = await KnowledgeExecutor(service, token_counter=lambda *_args: 1).execute(_request())
        projector = AgentCallResultProjector()
        values = {"capability_id": "knowledge.read_document", "output_payload": result.output_payload,
                  "call_item_id": "call", "outcome": "completed", "safe_error_code": None, "model_edition": "edition-a"}
        projected = await projector.project(**values)
        self.assertTrue(projected.accepted)
        self.assertEqual(projected.projection_mode, "knowledge_reference")
        self.assertFalse(projected.spill_required or projected.transient_stage_required)
        bad = await projector.project(**{**values, "output_payload": {**result.output_payload, "text": service.text}})
        self.assertFalse(bad.accepted)
        staged = await projector.project(**values, artifact_ids=("artifact",))
        self.assertFalse(staged.accepted)

    async def test_hydration_checks_live_acl_version_and_bound_identity(self):
        service = _Service()
        result = await KnowledgeExecutor(service, token_counter=lambda *_args: 1).execute(_request())
        items = _items(result.output_payload)
        resolver = KnowledgeResultResolver(service=service, scope_loader=lambda _run: SCOPE)
        values = {"run": _run(), "call_item": items[2], "result_item": items[3],
                  "durable_payload": json.loads(items[3].payload_json)}
        available = await resolver.resolve_tool_result(**values)
        self.assertIn("17%", available["safe_result"]["text"])
        service.text = "替换后正文"
        changed = await resolver.resolve_tool_result(**values)
        self.assertEqual(changed["safe_result"]["status"], "changed")
        self.assertNotIn("text", changed["safe_result"])
        service.allowed = False
        denied = await resolver.resolve_tool_result(**values)
        self.assertEqual(denied["safe_result"]["status"], "forbidden")
        with self.assertRaisesRegex(ValueError, "identity_invalid"):
            await resolver.resolve_tool_result(**{**values, "run": replace(_run(), task_id="another")})

    async def test_real_context_and_compaction_requests_load_and_count_full_body(self):
        service = _Service("唯一实验细节：增产17%。" + "正文\n" * 1500)
        result = await KnowledgeExecutor(service, token_counter=lambda *_args: 1).execute(_request())
        items = _items(result.output_payload, window=3000)
        resolver = KnowledgeResultResolver(service=service, scope_loader=lambda _run: SCOPE)
        builder = AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(AgentContextRules("system", "safe", "final"), knowledge_result_resolver=resolver),
            token_counter=lambda fragments, _binding: sum(map(len, fragments)),
        )
        candidate = await builder.build(run=_run(), items=items, catalog=AgentToolCatalog((), {}))
        self.assertIn("唯一实验细节", next(message.content for message in candidate.request.messages if message.role == "tool"))
        self.assertGreater(candidate.preflight.transient_tokens, len(service.text))
        self.assertEqual(candidate.preflight.decision, AgentContextPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE)
        compaction = AgentCompactionService(runs=None, writer=None, model=None, lease_controller=None,
                                            candidate_builder=builder, knowledge_result_resolver=resolver)
        request, _covered = await compaction._largest_fitting_request(run=_run(), all_items=items, covered=items, token_limit=20000)
        self.assertIn("唯一实验细节", request.messages[-1].content)
        self.assertIn("document_id", request.messages[-1].content)
        with self.assertRaisesRegex(RuntimeError, "required_segments_too_large"):
            await compaction._largest_fitting_request(run=_run(), all_items=items, covered=items, token_limit=1000)

    async def test_pending_batch_budget_counts_hydrated_earlier_result(self):
        service = _Service("长文🌾" * 400)
        result = await KnowledgeExecutor(service, token_counter=lambda *_args: 1).execute(_request())
        items = _items(result.output_payload, window=20000)
        next_call = _item("call-next", 5, AgentItemKind.TOOL_CALL,
                          {**json.loads(items[2].payload_json), "call_id": "next-provider"}, parent_item_id="assistant")
        reservation = _item("reserved", 6, AgentItemKind.TOOL_RESULT, {}, state=AgentItemState.RESERVED,
                            source_call_item_id="call-next")
        items += (next_call, reservation)
        builder = AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(AgentContextRules("system", "safe", "final"),
                knowledge_result_resolver=KnowledgeResultResolver(service=service, scope_loader=lambda _run: SCOPE)),
            token_counter=lambda fragments, _binding: sum(map(len, fragments)),
        )
        budget = await builder.knowledge_result_budget(run=_run(), items=items, catalog=AgentToolCatalog((), {}), call_item=next_call)
        self.assertLess(budget, 18000 - len(service.text))

    async def test_fresh_invocation_does_not_call_success_result_reuse(self):
        service = _Service()
        result = await KnowledgeExecutor(service, token_counter=lambda *_args: 1).execute(_request())
        items = _items(result.output_payload)
        node = TaskNode("node-1", "task-1", "knowledge.read_document", status=NodeStatus.COMPLETED)
        invocation = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(
            node=node, output_payload=result.output_payload, execution_result=CapabilityExecutionResult(
                "knowledge.read_document", "task-1", "node-1", result.output_payload))))
        invoker = AgentCapabilityInvoker(invocation_service=invocation,
            runs=SimpleNamespace(list_items=AsyncMock(return_value=items)),
            task_loader=AsyncMock(return_value=Task("task-1", "conv-1", "message-1", status=TaskStatus.RUNNING)),
            node_loader=AsyncMock(return_value=node), request_metadata_loader=lambda _run: METADATA,
            current_user_input_loader=lambda _run: "query")
        invoker._reuse_completed_result = AsyncMock(side_effect=AssertionError("must recheck current state"))
        outcome = await invoker.invoke(run=_run(), call=AgentToolCall("provider-call", "knowledge_read_document", "{}", 0),
            call_item=items[2], result_reservation=items[3], capability_id="knowledge.read_document",
            effective_payload={"library_id": "lib-1", "document_id": "doc-1"}, cancellation=None)
        self.assertEqual(outcome.status.value, "completed")
        invoker._reuse_completed_result.assert_not_awaited()
        invocation.invoke.assert_awaited_once()

    def test_catalog_is_fixed_native_and_only_enabled_for_scoped_runs(self):
        self.assertEqual({tool.capability_id for tool in _catalog().tools},
                         {"knowledge.list_libraries", "knowledge.read_index", "knowledge.read_document"})
        registry = CapabilityRegistry()
        for descriptor in KNOWLEDGE_CAPABILITY_DESCRIPTORS:
            self.assertEqual(descriptor.kind, "knowledge")
            registry.register(descriptor, invocation_policy=default_agent_invocation_policy(descriptor))
        self.assertEqual(AgentToolCatalogBuilder(registry).build(CapabilityVisibilityContext(OWNER)).tools, ())
        for tool in _catalog().tools:
            self.assertFalse(set(tool.input_schema["properties"]) & {"actor", "owner", "scope", "path"})
