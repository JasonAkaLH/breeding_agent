from __future__ import annotations

import _bootstrap  # noqa: F401
import json
import unittest

from src.core.contracts import CapabilityExecutionRequest
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from sql_query_skill.engine import SQLQueryEngine, SQLQueryEngineRequest


class SQLQueryEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_engine_runs_skill_domain_chain_and_strips_conversation_memory_from_llm_requests(self) -> None:
        prompts: list[str] = []
        seen_metadata: list[dict] = []

        async def llm_text_generator(prompt: str, *, request: CapabilityExecutionRequest | None = None) -> str:
            prompts.append(prompt)
            seen_metadata.append(dict(request.metadata if request is not None else {}))
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_generate" in prompt:
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_name"],
                        "column_types_used": {"variety.variety_name": "varchar(100)"},
                        "join_hints_used": [],
                    },
                    ensure_ascii=False,
                )
            if '"stage": "result_filtering"' in prompt:
                return json.dumps({"keep_row_indexes": [0], "filter_reason": "保留龙粳33。"}, ensure_ascii=False)
            raise AssertionError("unexpected SQLQuery LLM prompt")

        adapter = MySQLReadonlyAdapter(
            runner=lambda _sql: type(
                "Result",
                (),
                {"columns": ("variety_name",), "rows": ({"variety_name": "龙粳33"},), "row_count": 1},
            )()
        )
        engine = SQLQueryEngine(mysql_adapter=adapter, llm_text_generator=llm_text_generator)

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:skill_execute",
                metadata={
                    "conversation_memory": {"history_summary": "secret memory"},
                    "memory_context": {"recent_messages": ["secret"]},
                    "history_summary": "secret",
                    "resolved_user_message": "secret",
                    "deep_thinking": True,
                    "main_agent_reasoning_effort": "high",
                },
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["domain_kind"], "sql_query")
        self.assertEqual(result.output_payload["capability_id"], "skill.sql_query")
        self.assertEqual(result.output_payload["filtered_query_result"]["row_count"], 1)
        self.assertEqual(len(prompts), 2)
        self.assertTrue(seen_metadata)
        blocked_keys = {"conversation_memory", "memory_context", "history_summary", "resolved_user_message"}
        for metadata in seen_metadata:
            self.assertFalse(blocked_keys & set(metadata))
            self.assertEqual(metadata["main_agent_reasoning_effort"], "high")
        self.assertEqual(seen_metadata[0]["deep_thinking"], True)
        self.assertEqual(seen_metadata[1]["deep_thinking"], True)
        self.assertTrue(any(event.event_type == "skill.progress" for event in result.events))
        self.assertTrue(all(artifact.producer_node_id == "task-1:skill_execute" for artifact in result.artifacts))

    async def test_engine_records_progress_live_when_recorder_is_available(self) -> None:
        recorded_progress = []

        async def progress_recorder(event):
            recorded_progress.append(event)

        async def llm_text_generator(prompt: str, **_: object) -> str:
            self.assertTrue(recorded_progress)
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_generate" in prompt:
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_name"],
                        "column_types_used": {"variety.variety_name": "varchar(100)"},
                        "join_hints_used": [],
                    },
                    ensure_ascii=False,
                )
            if '"stage": "result_filtering"' in prompt:
                return json.dumps({"keep_row_indexes": [0], "filter_reason": "保留龙粳33。"}, ensure_ascii=False)
            raise AssertionError("unexpected SQLQuery LLM prompt")

        adapter = MySQLReadonlyAdapter(
            runner=lambda _sql: type(
                "Result",
                (),
                {"columns": ("variety_name",), "rows": ({"variety_name": "龙粳33"},), "row_count": 1},
            )()
        )
        engine = SQLQueryEngine(mysql_adapter=adapter, llm_text_generator=llm_text_generator, progress_event_recorder=progress_recorder)

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-live-progress",
                node_id="task-live-progress:skill_execute",
                metadata={},
            )
        )

        self.assertIsNone(result.error)
        self.assertGreaterEqual(len(recorded_progress), 6)
        self.assertEqual(recorded_progress[0].payload["stage"], "intent_route")
        self.assertEqual(recorded_progress[0].payload["skill_name"], "sql-query")
        self.assertFalse(any(event.event_type == "skill.progress" for event in result.events))

    async def test_unsafe_llm_generated_sql_still_flows_through_guard(self) -> None:
        async def unsafe_llm(prompt: str, **_: object) -> str:
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_generate" in prompt:
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety.variety_id FROM variety; SELECT variety.variety_id FROM variety",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_id"],
                        "column_types_used": {"variety.variety_id": "int(11)"},
                        "join_hints_used": [],
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)

        adapter = MySQLReadonlyAdapter(
            runner=lambda _sql: type("Result", (), {"columns": (), "rows": (), "row_count": 0})()
        )
        engine = SQLQueryEngine(mysql_adapter=adapter, llm_text_generator=unsafe_llm)

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-guard",
                node_id="task-guard:skill_execute",
                metadata={},
            )
        )

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.code, "multiple_statements")
        self.assertTrue(any(event.event_type == "skill.sql_guard_blocked" for event in result.events))


if __name__ == "__main__":
    unittest.main()
