#!/usr/bin/env python3
"""Explicit live literature evaluation in a disposable local knowledge/runtime store.

The corpus manifest contains libraries[{id,name,description,documents[{id,path}]}]
and questions[{id,text,library_ids,expected_document_ids,expected_facts,
require_compaction}]. Document paths are relative to the manifest directory.
The JSON report contains counts and checks, never document text, questions,
answers, request payloads, credentials or local paths. Human review of citation
meaning and unsupported claims is still required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {"libraries", "questions"}:
        raise ValueError("knowledge_evaluation_manifest_invalid")
    libraries, questions = manifest["libraries"], manifest["questions"]
    if not isinstance(libraries, list) or not libraries or not isinstance(questions, list) or not questions:
        raise ValueError("knowledge_evaluation_manifest_empty")
    library_ids, document_ids, question_ids = set(), set(), set()

    def identifier(value, seen):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) or value in seen:
            raise ValueError("knowledge_evaluation_identifier_invalid")
        seen.add(value)

    for library in libraries:
        identifier(library["id"], library_ids)
        if not isinstance(library["name"], str) or not library["name"].strip():
            raise ValueError("knowledge_evaluation_library_invalid")
        if not isinstance(library.get("description", ""), str) or not isinstance(library["documents"], list):
            raise ValueError("knowledge_evaluation_library_invalid")
        for document in library["documents"]:
            identifier(document["id"], document_ids)
            candidate = (path.parent / document["path"]).resolve()
            if not candidate.is_relative_to(path.parent.resolve()) or candidate.suffix.lower() != ".md" or not candidate.is_file():
                raise ValueError("knowledge_evaluation_document_invalid")
            document["resolved_path"] = candidate
    for question in questions:
        identifier(question["id"], question_ids)
        if not isinstance(question["text"], str) or not question["text"].strip():
            raise ValueError("knowledge_evaluation_question_invalid")
        for key, allowed in (("library_ids", library_ids), ("expected_document_ids", document_ids)):
            values = question.get(key, [])
            if not isinstance(values, list) or len(values) != len(set(values)) or any(value not in allowed for value in values):
                raise ValueError("knowledge_evaluation_reference_invalid")
        facts = question.get("expected_facts", [])
        if not isinstance(facts, list) or any(not isinstance(fact, str) or not fact for fact in facts):
            raise ValueError("knowledge_evaluation_facts_invalid")
        if not isinstance(question.get("require_compaction", False), bool):
            raise ValueError("knowledge_evaluation_compaction_invalid")
    return manifest


class _MeasuredModel:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = []

    async def sample_agent(self, request):
        started = time.perf_counter()
        result = await self.delegate.sample_agent(request)
        self.calls.append({
            "compaction": request.request_id.startswith("agent-compaction:"),
            "seconds": round(time.perf_counter() - started, 3),
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "usage_status": result.usage.status,
        })
        return result


async def evaluate(args, manifest: dict) -> dict:
    from unittest.mock import patch

    from src.api.dto import SubmitMessageRequest
    from src.api.runtime import build_api_runtime
    from src.core.enums import MessageRole
    from src.orchestration.agent_loop.models import AgentItemKind

    with tempfile.TemporaryDirectory(prefix="knowledge-evaluation-") as temporary:
        root = Path(temporary)
        # Explicitly isolate evaluation from any configured application database and Sidecar.
        with patch.dict("os.environ", {"MAF_STATE_STORE_BACKEND": "sqlite", "MAF_RUST_RUNTIME_STORE_MODE": "off"}):
            runtime = build_api_runtime(
                database_path=root / "evaluation.sqlite", audit_log_path=root / "audit.jsonl",
                master_key_bytes=os.urandom(32),
                knowledge_store_path=root / "knowledge", main_agent_llm_config_path=Path(args.config),
                skill_roots=[], enable_conversation_memory=False,
                enable_conversation_title_llm=False,
                enable_user_mcp=False, enable_user_mcp_routing=False,
            )
        started = time.perf_counter()
        try:
            owner = "knowledge-evaluation"
            library_map, document_map, titles = {}, {}, {}
            extraction_calls = []
            llm_runtime = runtime.knowledge_service.extractor.llm_runtime
            generate_text = llm_runtime.generate_text

            async def measured_extraction(*args, **kwargs):
                call_started = time.perf_counter()
                result = await generate_text(*args, **kwargs)
                extraction_calls.append(round(time.perf_counter() - call_started, 3))
                return result

            llm_runtime.generate_text = measured_extraction
            ingestion_started = time.perf_counter()
            for library in manifest["libraries"]:
                created = await runtime.knowledge_service.create_library(owner, library["name"], library.get("description", ""))
                library_map[library["id"]] = created["library_id"]
                for document in library["documents"]:
                    imported = (await runtime.knowledge_service.upload_imports(
                        owner, created["library_id"], [("literature.md", document["resolved_path"].read_bytes())],
                    ))[0]
                    await runtime.knowledge_service.process_pending()
                    draft = await runtime.knowledge_service.get_import(owner, created["library_id"], imported["import_id"])
                    if draft["status"] != "awaiting_review":
                        raise ValueError("knowledge_evaluation_extraction_failed")
                    # --live explicitly authorizes publishing extraction results only in this temporary store.
                    published = await runtime.knowledge_service.confirm_import(
                        owner, created["library_id"], imported["import_id"], expected_revision=draft["revision"],
                    )
                    document_map[document["id"]] = published["document_id"]
                    titles[document["id"]] = published["title"]
            ingestion_seconds = round(time.perf_counter() - ingestion_started, 3)
            runner = runtime.agent_loop_orchestrator._runner
            measured = _MeasuredModel(runner._model)
            runner._model = measured
            runner._compaction_service._model = measured
            results = []
            for question in manifest["questions"]:
                conversation_id = "knowledge-evaluation-" + question["id"]
                selected = [library_map[key] for key in question.get("library_ids", [])]
                await runtime.save_knowledge_selection(
                    owner, conversation_id, "selected" if selected else "auto", selected,
                )
                call_start, question_started = len(measured.calls), time.perf_counter()
                _, task = await runtime.submit_message(
                    conversation_id, SubmitMessageRequest(conversation_id=conversation_id, content=question["text"], metadata={"deep_thinking": False}),
                    authenticated_username=owner,
                )
                deadline = asyncio.get_running_loop().time() + args.timeout
                while True:
                    current = await runtime.storage.get_task(task.task_id)
                    if current is not None and str(current.status) in {"completed", "failed", "cancelled"}:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError("knowledge_evaluation_timeout")
                    await asyncio.sleep(0.1)
                run = await runtime.agent_task_projection.get_agent_run(task.task_id)
                items = await runtime.agent_run_repository.list_items(run.run_id)
                ranges, read_libraries = {}, set()
                for item in items:
                    if item.kind is not AgentItemKind.TOOL_RESULT:
                        continue
                    result = json.loads(item.payload_json).get("safe_result", {})
                    reference = result.get("reference") or {}
                    if result.get("status") == "available" and reference.get("library_id"):
                        read_libraries.add(reference["library_id"])
                    if result.get("status") == "available" and reference.get("capability_id") == "knowledge.read_document":
                        ranges.setdefault(reference["document_id"], []).append(reference)
                await runtime.sync_assistant_history_messages(conversation_id)
                messages = await runtime.storage.list_messages_for_conversation(conversation_id)
                messages = await runtime.agent_task_projection.project_history_messages(conversation_id, messages)
                answer = "\n".join(message.content for message in messages if str(message.role) == str(MessageRole.ASSISTANT))
                expected = {document_map[key] for key in question.get("expected_document_ids", [])}
                complete = set()
                for document_id, pages in ranges.items():
                    offset = 0
                    for page in sorted(pages, key=lambda page: page["start"]):
                        if page["start"] > offset:
                            break
                        offset = max(offset, page["end"])
                    if offset == pages[0]["total_characters"]:
                        complete.add(document_id)
                calls = measured.calls[call_start:]
                results.append({
                    "question_id": question["id"], "status": str(current.status),
                    "seconds": round(time.perf_counter() - question_started, 3),
                    "model_calls": len(calls), "compaction_calls": sum(call["compaction"] for call in calls),
                    "prompt_tokens": sum(call["prompt_tokens"] or 0 for call in calls),
                    "completion_tokens": sum(call["completion_tokens"] or 0 for call in calls),
                    "all_usage_available": all(call["usage_status"] == "available" for call in calls),
                    "documents_read": len(ranges), "documents_completely_read": len(complete),
                    "library_ids_read": sorted(key for key, value in library_map.items() if value in read_libraries),
                    "document_ids_read": sorted(key for key, value in document_map.items() if value in ranges),
                    "expected_documents_read": expected.issubset(ranges),
                    "expected_documents_completely_read": expected.issubset(complete),
                    "expected_facts_present": all(fact.casefold() in answer.casefold() for fact in question.get("expected_facts", [])),
                    "citation_marker_present": bool(re.search(r"\[\d+\]", answer)),
                    "expected_bibliographic_titles_present": all(titles[key] in answer for key in question.get("expected_document_ids", [])),
                    "required_compaction_observed": not question.get("require_compaction", False) or any(call["compaction"] for call in calls),
                    "citation_meaning_requires_human_review": True,
                })
            return {
                "schema": "knowledge_evaluation.v1", "live": True,
                "libraries": len(library_map), "documents": len(document_map),
                "ingestion_seconds": ingestion_seconds,
                "ingestion_model_calls": len(extraction_calls),
                "ingestion_model_usage": "not_available_from_text_adapter",
                "total_seconds": round(time.perf_counter() - started, 3), "results": results,
            }
        finally:
            await runtime.shutdown()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Explicit authorized LLM configuration; never printed")
    parser.add_argument("--corpus", required=True, help="Explicit authorized corpus manifest")
    parser.add_argument("--live", action="store_true", help="Allow actual provider calls and temporary import confirmation")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(Path(args.corpus))
        if not args.live:
            print(json.dumps({"live": False, "libraries": len(manifest["libraries"]), "questions": len(manifest["questions"])}))
            return 0
        report = asyncio.run(evaluate(args, manifest))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        checks = ("expected_documents_read", "expected_documents_completely_read", "expected_facts_present", "citation_marker_present", "expected_bibliographic_titles_present", "required_compaction_observed")
        return 0 if all(result["status"] == "completed" and all(result[key] for key in checks) for result in report["results"]) else 2
    except Exception:
        print(json.dumps({"error": "knowledge_evaluation_failed", "private_details_omitted": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
