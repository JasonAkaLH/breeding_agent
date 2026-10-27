from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

from src.capabilities.knowledge.executor import KnowledgeExecutor
from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentItemKind,
    AgentSample,
    AgentToolCall,
    AgentUsage,
)
from tests.api.support import test_llm_config
from tests.e2e.support import E2EAPITestCase


EARLY_FACT = "EARLY_EXPERIMENT: the seeds received exactly 17 hours of light."
TAIL_FACT = "TAIL_EXPERIMENT: the final yield increased by exactly 23 percent."
PRIVATE_PARAGRAPH = "CONTROL_PRIVATE_BODY: measured seedling observations, not index metadata.\n"
ANSWER = (
    "文献支持：光照为17小时，最终产量提高23% [1]。\n\n"
    "[1] Test Author. Light and yield experiment. 2026. DOI: 10.0000/knowledge-test.\n\n"
    "通用知识补充：其他育种条件仍需单独验证。"
)


class _ReadingModel:
    """Use only actual model requests to drive the deterministic reading sequence."""

    def __init__(self, library_id: str, document_id: str) -> None:
        self.library_id = library_id
        self.document_id = document_id
        self.requests = []
        self.pages = defaultdict(dict)
        self.compaction_requests = []
        self.index_complete = False

    @staticmethod
    def _render(request) -> str:
        return "\n".join(message.content or "" for message in request.messages)

    def _sample(self, request, *, text: str = "", capability_id=None, arguments=None):
        calls = ()
        if capability_id is not None:
            tool = next(tool for tool in request.tools if tool.capability_id == capability_id)
            calls = (
                AgentToolCall(
                    f"knowledge-call-{len(self.requests)}",
                    tool.provider_safe_name,
                    json.dumps(arguments or {}, ensure_ascii=False),
                    0,
                ),
            )
        return AgentSample(
            f"knowledge-sample-{len(self.requests)}", request.binding, text, calls,
            AgentUsage(status="usage_unavailable"),
            AgentFinishMetadata("tool_calls" if calls else "stop", 1),
        )

    async def sample_agent(self, request):
        self.requests.append(request)
        rendered = self._render(request)
        if request.request_id.startswith("agent-compaction:"):
            self.compaction_requests.append(request)
            # Preserve only evidence actually present in this compression request.
            facts = []
            for fact in (EARLY_FACT, TAIL_FACT):
                if fact in rendered:
                    facts.append(fact)
            return self._sample(
                request,
                text=(
                    f"Read literature {self.document_id} in library {self.library_id}; "
                    "retain the original read ranges and continue any remaining pages. "
                    + " ".join(facts)
                ),
            )
        views = []
        for message in request.messages:
            if message.role != "tool":
                continue
            payload = json.loads(message.content)
            view = payload.get("safe_result", {})
            if view.get("schema") == "maf.agent.knowledge_content.v1":
                if view.get("status") != "available":
                    raise AssertionError(f"Unexpected knowledge status: {view.get('status')}")
                views.append(view)
        if not views:
            return self._sample(request, capability_id="knowledge.list_libraries")
        for view in views:
            ref = view["reference"]
            self.pages[ref["capability_id"]][ref["start"]] = view
        latest = views[-1]
        ref = latest["reference"]
        capability_id = ref["capability_id"]
        if capability_id == "knowledge.list_libraries":
            if self.library_id not in latest["text"]:
                raise AssertionError("The selected library did not reach the model")
            next_capability = "knowledge.read_index"
            arguments = {"library_id": self.library_id}
        elif capability_id == "knowledge.read_index":
            if ref["next_cursor"]:
                next_capability = capability_id
                arguments = {"library_id": self.library_id, "cursor": ref["next_cursor"]}
            else:
                index = "".join(
                    view["text"] for _, view in sorted(self.pages[capability_id].items())
                )
                if self.document_id not in index or "Light and yield experiment" not in index:
                    raise AssertionError("The model must receive the document's index entry first")
                self.index_complete = True
                next_capability = "knowledge.read_document"
                arguments = {"library_id": self.library_id, "document_id": self.document_id}
        else:
            if not self.index_complete:
                raise AssertionError("Document content arrived before the complete index")
            if ref["next_cursor"]:
                next_capability = capability_id
                arguments = {
                    "library_id": self.library_id, "document_id": self.document_id,
                    "cursor": ref["next_cursor"],
                }
            else:
                if EARLY_FACT not in rendered or TAIL_FACT not in rendered:
                    raise AssertionError("Final synthesis lost the early or final full-text fact")
                if latest["metadata"] != {
                    "title": "Light and yield experiment", "authors": ["Test Author"],
                    "year": 2026, "doi": "10.0000/knowledge-test",
                }:
                    raise AssertionError("Bibliographic metadata did not reach the model")
                return self._sample(request, text=ANSWER)
        return self._sample(request, capability_id=next_capability, arguments=arguments)


class KnowledgeFlowE2ETest(E2EAPITestCase):
    async def _configure(self, *, context_window: int = 1_024_000) -> None:
        config = test_llm_config()
        config["model_editions"]["options"][0]["trim_max_tokens"] = context_window
        await self.reconfigure_runtime(
            main_agent_llm_config=config,
            main_agent_stream_generator=lambda _prompt, **_kwargs: "unused model adapter",
            enable_conversation_memory=False,
        )
        self.extracted = []

        async def extract(text):
            self.extracted.append(text)
            return {
                "title": "Light and yield experiment" if EARLY_FACT in text else "Other experiment",
                "authors": ["Test Author"], "year": 2026,
                "doi": "10.0000/knowledge-test", "keywords": ["light", "yield"],
                "summary": "Research on light treatment and yield, with full experimental methods.",
            }

        self.runtime.knowledge_service.extractor = extract
        runner = self.runtime.agent_loop_orchestrator._runner
        self.counted_fragments = []

        def count(fragments, _binding):
            self.counted_fragments.extend(fragments)
            return sum(len(fragment) for fragment in fragments)

        runner._context_candidate_builder._count = count
        self.executor = next(
            executor for executor in runner._invoker._invocation._executor._executors
            if isinstance(executor, KnowledgeExecutor)
        )
        # Explicit deterministic token units make large-file and compaction boundaries reproducible.
        self.executor._token_counter = lambda text, _edition: len(text) * 4

    async def _create_library(self):
        response = await self.client.post(
            "/api/v1/knowledge/libraries",
            json={"name": "Light experiments", "description": "Breeding light and yield trials"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["library_id"]

    async def _publish(self, library_id: str, raw: bytes, filename="experiment.md"):
        response = await self.client.post(
            f"/api/v1/knowledge/libraries/{library_id}/imports",
            files=[("files", (filename, raw, "application/octet-stream"))],
        )
        self.assertEqual(response.status_code, 202, response.text)
        import_id = response.json()["imports"][0]["import_id"]
        await self.runtime.knowledge_service.process_pending()
        draft_response = await self.client.get(
            f"/api/v1/knowledge/libraries/{library_id}/imports/{import_id}"
        )
        self.assertEqual(draft_response.status_code, 200, draft_response.text)
        draft = draft_response.json()
        self.assertEqual(draft["status"], "awaiting_review")
        index = await self.client.get(f"/api/v1/knowledge/libraries/{library_id}/index")
        self.assertNotIn(import_id, index.json()["text"])
        self.assertNotIn(draft["title"], index.json()["text"])
        response = await self.client.post(
            f"/api/v1/knowledge/libraries/{library_id}/imports/{import_id}/confirm",
            json={"revision": draft["revision"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["document_id"]

    def _bind_reading_model(self, library_id, document_id):
        model = _ReadingModel(library_id, document_id)
        runner = self.runtime.agent_loop_orchestrator._runner
        runner._model = model
        runner._compaction_service._model = model
        return model

    async def _ask(self, library_id: str, conversation_id: str):
        selected = await self.client.put(
            f"/api/v1/conversations/{conversation_id}/knowledge-selection",
            json={"mode": "selected", "library_ids": [library_id]},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": conversation_id,
                "content": "根据库内试验全文，光照时长及最终产量变化是多少？请附文献引用。",
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"], timeout=20)
        self.assertEqual(terminal["status"], "completed", terminal)
        return response.json()["task_id"]

    @staticmethod
    def _assert_coverage(test, pages, text):
        ordered = [view for _, view in sorted(pages.items())]
        offset = 0
        for view in ordered:
            ref = view["reference"]
            test.assertEqual(ref["start"], offset)
            test.assertEqual(view["text"], text[ref["start"]:ref["end"]])
            offset = ref["end"]
        test.assertEqual(offset, len(text))
        test.assertTrue(ordered[-1]["complete"])
        test.assertIsNone(ordered[-1]["reference"]["next_cursor"])

    async def test_approved_raw_body_reaches_model_after_index_and_answer_survives_delete(self):
        await self._approved_body_chain(pdf=False)

    async def test_approved_pdf_text_reaches_model_after_index_and_answer_survives_delete(self):
        await self._approved_body_chain(pdf=True)

    async def _approved_body_chain(self, *, pdf):
        await self._configure()
        library_id = await self._create_library()
        raw = ("\ufeff# Light and yield experiment\r\n\r\n" + EARLY_FACT + "\r\n" + TAIL_FACT).encode()
        expected_text = raw.decode("utf-8-sig")
        if pdf:
            from src.capabilities.knowledge.pdf import parse_pdf
            from tests.capabilities.knowledge.pdf_fixtures import text_pdf
            raw = text_pdf(EARLY_FACT, TAIL_FACT)
            expected_text = parse_pdf(raw)
        document_id = await self._publish(library_id, raw, "experiment.pdf" if pdf else "experiment.md")
        self.assertIn(EARLY_FACT, self.extracted[0])
        self.assertIn(TAIL_FACT, self.extracted[0])
        download = await self.client.get(
            f"/api/v1/knowledge/libraries/{library_id}/documents/{document_id}/content?download=true"
        )
        self.assertEqual(download.content, expected_text.encode() if pdf else raw)
        self.assertEqual(download.headers["x-content-sha256"], hashlib.sha256(download.content).hexdigest())
        model = self._bind_reading_model(library_id, document_id)
        task_id = await self._ask(library_id, "conv-knowledge-chain")
        self.assertEqual(
            [view["reference"]["capability_id"] for view in (
                next(iter(pages.values())) for pages in model.pages.values()
            )],
            ["knowledge.list_libraries", "knowledge.read_index", "knowledge.read_document"],
        )
        self._assert_coverage(self, model.pages["knowledge.read_document"], expected_text)
        history_url = "/api/v1/conversations/conv-knowledge-chain/messages"
        messages = (await self.client.get(history_url)).json()["messages"]
        answer = next(message["content"] for message in messages if message["role"] == "assistant")
        self.assertEqual(answer, ANSWER)
        run = await self.runtime.agent_task_projection.get_agent_run(task_id)
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        for item in items:
            if item.kind is AgentItemKind.TOOL_RESULT:
                self.assertLess(len(item.payload_json.encode()), 131_072)
                payload = json.loads(item.payload_json)
                self.assertNotIn(EARLY_FACT, item.payload_json)
                self.assertNotIn("text", payload["safe_result"])
        artifacts = (await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")).json()["artifacts"]
        self.assertEqual([artifact["storage_ref"] for artifact in artifacts], [ANSWER])
        self.assertTrue(all(artifact["filename"] is None for artifact in artifacts))
        response = await self.client.delete(
            f"/api/v1/knowledge/libraries/{library_id}/documents/{document_id}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        download = await self.client.get(
            f"/api/v1/knowledge/libraries/{library_id}/documents/{document_id}/content"
        )
        self.assertIn(download.status_code, (403, 404, 410))
        after = (await self.client.get(history_url)).json()["messages"]
        self.assertEqual(after, messages)

    async def test_long_index_full_body_and_compaction_use_actual_ranges(self):
        await self._long_body_compaction(pdf=False)

    async def test_long_pdf_full_body_and_compaction_use_actual_ranges(self):
        await self._long_body_compaction(pdf=True)

    async def test_pdf_pages_leave_room_for_compaction_with_matching_token_counters(self):
        await self._long_body_compaction(pdf=True, matching_counters=True)

    async def _long_body_compaction(self, *, pdf, matching_counters=False):
        await self._configure(context_window=40_000)
        if matching_counters:
            self.executor._token_counter = lambda text, _edition: len(text)
        library_id = await self._create_library()
        raw = ("# Light and yield experiment\n" + EARLY_FACT + "\n" + PRIVATE_PARAGRAPH * 1_850 + TAIL_FACT).encode()
        expected_text = raw.decode()
        if pdf:
            from src.capabilities.knowledge.pdf import parse_pdf
            from tests.capabilities.knowledge.pdf_fixtures import text_pdf
            raw = text_pdf(EARLY_FACT, *[PRIVATE_PARAGRAPH * 15 for _ in range(124)], TAIL_FACT)
            expected_text = parse_pdf(raw)
        document_id = await self._publish(library_id, raw, "long.pdf" if pdf else "long.md")
        service = self.runtime.knowledge_service
        for number in range(28):
            imported = (await service.upload_imports("acc-1", library_id, [(f"other-{number}.md", f"# Other {number}\nMethods".encode())]))[0]
            await service.process_pending()
            draft = await service.get_import("acc-1", library_id, imported["import_id"])
            metadata = {
                "title": f"Other light trial {number}", "authors": [], "year": None, "doi": None,
                "keywords": [], "summary": (f"Other trial {number} methods and findings. " * 30),
            }
            draft = await service.edit_import("acc-1", library_id, imported["import_id"], metadata, expected_revision=draft["revision"])
            await service.confirm_import("acc-1", library_id, imported["import_id"], expected_revision=draft["revision"])
        index = (await self.client.get(f"/api/v1/knowledge/libraries/{library_id}/index")).json()["text"]
        self.assertGreater(len(raw), 120_000)
        self.assertGreater(len(index) * 4, 50_000)
        model = self._bind_reading_model(library_id, document_id)
        task_id = await self._ask(library_id, "conv-knowledge-long")
        self.assertGreater(len(model.pages["knowledge.read_index"]), 1)
        self.assertGreater(len(model.pages["knowledge.read_document"]), 1)
        self._assert_coverage(self, model.pages["knowledge.read_index"], index)
        self._assert_coverage(self, model.pages["knowledge.read_document"], expected_text)
        self.assertTrue(model.compaction_requests, "The scenario must actually compact")
        self.assertTrue(any(EARLY_FACT in model._render(request) for request in model.compaction_requests))
        self.assertTrue(any(EARLY_FACT in fragment for fragment in self.counted_fragments))
        self.assertTrue(any(PRIVATE_PARAGRAPH.strip() in fragment for fragment in self.counted_fragments))
        run = await self.runtime.agent_task_projection.get_agent_run(task_id)
        self.assertGreater(run.compacted_through_sequence, 0)
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        for item in items:
            if item.kind is AgentItemKind.TOOL_RESULT:
                self.assertNotIn(PRIVATE_PARAGRAPH.strip(), item.payload_json)
                self.assertLess(len(item.payload_json.encode()), 131_072)
        audit = (self.workspace / "audit.jsonl").read_text()
        self.assertNotIn(PRIVATE_PARAGRAPH.strip(), audit)
        with sqlite3.connect(f"file:{self.workspace / 'phase6-api.sqlite3'}?mode=ro", uri=True) as connection:
            self.assertNotIn(PRIVATE_PARAGRAPH.strip(), "\n".join(connection.iterdump()))
        messages = (await self.client.get("/api/v1/conversations/conv-knowledge-long/messages")).json()["messages"]
        self.assertEqual(next(message["content"] for message in messages if message["role"] == "assistant"), ANSWER)

    async def test_live_evaluation_harness_reports_safe_metrics_with_fake_provider(self):
        from scripts.evaluate_knowledge import evaluate, load_manifest

        await self._configure()
        model = self._bind_reading_model("pending", "pending")
        service = self.runtime.knowledge_service
        original_confirm = service.confirm_import

        async def confirm(*args, **kwargs):
            result = await original_confirm(*args, **kwargs)
            model.library_id, model.document_id = result["library_id"], result["document_id"]
            return result

        service.confirm_import = confirm

        class Extractor:
            def __init__(inner):
                async def generate(_text):
                    return json.dumps({
                        "title": "Light and yield experiment", "authors": ["Test Author"],
                        "year": 2026, "doi": "10.0000/knowledge-test", "keywords": [],
                        "summary": "Light and yield experiment, including methods outside its abstract.",
                    })
                inner.llm_runtime = SimpleNamespace(generate_text=generate)

            async def __call__(inner, text):
                return json.loads(await inner.llm_runtime.generate_text(text))

        service.extractor = Extractor()
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf
        corpus_file = self.workspace / "paper.pdf"
        corpus_file.write_bytes(text_pdf(EARLY_FACT, TAIL_FACT))
        manifest_file = self.workspace / "corpus.json"
        manifest_file.write_text(json.dumps({
            "libraries": [{"id": "rice", "name": "Light experiments", "documents": [{"id": "paper1", "path": "paper.pdf"}, {"id": "same-source", "path": "paper.pdf"}]}],
            "questions": [{"id": "q1", "text": "Report full-text experimental results", "library_ids": ["rice"], "expected_document_ids": ["paper1"], "expected_facts": ["17", "23"]}],
        }))
        with patch("src.api.runtime.build_api_runtime", return_value=self.runtime):
            report = await evaluate(SimpleNamespace(config="unused.yaml", timeout=10), load_manifest(manifest_file))
        result = report["results"][0]
        self.assertEqual(report["ingestion_model_calls"], 1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["library_ids_read"], ["rice"])
        self.assertEqual(result["document_ids_read"], ["paper1", "same-source"])
        self.assertTrue(result["expected_documents_completely_read"])
        self.assertTrue(result["expected_facts_present"], result)
        self.assertTrue(result["expected_bibliographic_titles_present"])
        self.assertFalse(result["all_usage_available"])
        self.assertEqual(result["model_calls"], 4)
        rendered = json.dumps(report)
        for private in (str(self.workspace), EARLY_FACT, TAIL_FACT, ANSWER, "unused.yaml"):
            self.assertNotIn(private, rendered)
