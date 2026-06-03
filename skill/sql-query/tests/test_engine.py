from __future__ import annotations

import _bootstrap  # noqa: F401
import json
import unittest

from src.core.contracts import CapabilityExecutionRequest, CapabilityExecutionResult
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from sql_query_skill.engine import SQLQueryEngine, SQLQueryEngineRequest
from support import fake_query_result


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

    async def test_engine_repairs_repairable_execution_error_once(self) -> None:
        prompts: list[str] = []
        executed_sql: list[str] = []

        async def llm_text_generator(prompt: str, **_: object) -> str:
            prompts.append(prompt)
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_repair" in prompt:
                self.assertIn("SELECT variety_name FROM variety WHERE", prompt)
                self.assertIn("db_sql_syntax_error", prompt)
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_name"],
                        "column_types_used": {"variety.variety_id": "int(11)"},
                        "join_hints_used": [],
                    },
                    ensure_ascii=False,
                )
            if "当前阶段：sql_generate" in prompt:
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety_name FROM variety WHERE",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_name"],
                        "column_types_used": {"variety.variety_name": "varchar(100)"},
                        "join_hints_used": [],
                    },
                    ensure_ascii=False,
                )
            if '"stage": "result_filtering"' in prompt:
                self.assertIn("龙粳33", prompt)
                return json.dumps({"keep_row_indexes": [0], "filter_reason": "保留修复后结果。"}, ensure_ascii=False)
            raise AssertionError(f"unexpected SQLQuery LLM prompt: {prompt[:120]}")

        class RepairAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None):
                executed_sql.append(sql)
                if len(executed_sql) == 1:
                    raise RuntimeError("(1064, \"You have an error in your SQL syntax near ''\")")
                return fake_query_result(rows=({"variety_name": "龙粳33"},))

        engine = SQLQueryEngine(mysql_adapter=RepairAdapter(), llm_text_generator=llm_text_generator)  # type: ignore[arg-type]

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-repair",
                node_id="task-repair:skill_execute",
                metadata={},
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(len(executed_sql), 2)
        self.assertEqual(result.output_payload["sql_repair"]["attempts"], 1)
        self.assertEqual(result.output_payload["filtered_query_result"]["rows"], [{"variety_name": "龙粳33"}])
        self.assertTrue(any(event.event_type == "skill.sql_repair_attempted" for event in result.events))
        self.assertTrue(any(event.event_type == "skill.sql_repair_succeeded" for event in result.events))
        self.assertEqual(sum("当前阶段：sql_repair" in prompt for prompt in prompts), 1)
        repair_event_payloads = [
            json.dumps(event.payload, ensure_ascii=False).lower()
            for event in result.events
            if event.event_type.startswith("skill.sql_repair_")
        ]
        self.assertTrue(repair_event_payloads)
        self.assertFalse(
            any(
                "password" in payload or "guard:ok" in payload or "failed_sql" in payload
                for payload in repair_event_payloads
            )
        )
        generated_sql_artifacts = [artifact for artifact in result.artifacts if "generated_sql" in artifact.artifact_id]
        self.assertGreaterEqual(len(generated_sql_artifacts), 2)
        self.assertEqual(len({artifact.artifact_id for artifact in generated_sql_artifacts}), len(generated_sql_artifacts))
        self.assertTrue(all(artifact.producer_node_id == "task-repair:skill_execute" for artifact in generated_sql_artifacts))

    async def test_engine_does_not_repair_multiple_statements(self) -> None:
        prompts: list[str] = []

        async def unsafe_llm(prompt: str, **_: object) -> str:
            prompts.append(prompt)
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
            if "当前阶段：sql_repair" in prompt:
                raise AssertionError("unsafe guard failures must not trigger repair")
            return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)

        class FailingIfExecutedAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None):
                raise AssertionError("unsafe SQL must not be executed")

        engine = SQLQueryEngine(mysql_adapter=FailingIfExecutedAdapter(), llm_text_generator=unsafe_llm)  # type: ignore[arg-type]

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-no-repair-unsafe",
                node_id="task-no-repair-unsafe:skill_execute",
                metadata={},
            )
        )

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.code, "multiple_statements")
        self.assertFalse(any("当前阶段：sql_repair" in prompt for prompt in prompts))

    async def test_repaired_sql_is_reguarded_before_execution(self) -> None:
        executed_sql: list[str] = []

        async def llm_text_generator(prompt: str, **_: object) -> str:
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_repair" in prompt:
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
            if "当前阶段：sql_generate" in prompt:
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety_name FROM variety WHERE",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_name"],
                        "column_types_used": {"variety.variety_name": "varchar(100)"},
                        "join_hints_used": [],
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)

        class RepairToUnsafeAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None):
                executed_sql.append(sql)
                raise RuntimeError("(1064, \"You have an error in your SQL syntax\")")

        engine = SQLQueryEngine(mysql_adapter=RepairToUnsafeAdapter(), llm_text_generator=llm_text_generator)  # type: ignore[arg-type]

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-repair-unsafe",
                node_id="task-repair-unsafe:skill_execute",
                metadata={},
            )
        )

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.code, "multiple_statements")
        self.assertEqual(executed_sql, ["SELECT variety_name FROM variety WHERE"])
        self.assertTrue(any(event.event_type == "skill.sql_repair_failed" for event in result.events))

    async def test_engine_repair_attempt_limit_is_one(self) -> None:
        executed_sql: list[str] = []
        repair_prompt_count = 0

        async def llm_text_generator(prompt: str, **_: object) -> str:
            nonlocal repair_prompt_count
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_repair" in prompt:
                repair_prompt_count += 1
                sql = "SELECT variety_name FROM variety WHERE variety_id ="
            elif "当前阶段：sql_generate" in prompt:
                sql = "SELECT variety_name FROM variety WHERE"
            else:
                return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": sql,
                    "tables_used": ["variety"],
                    "columns_used": ["variety.variety_name", "variety.variety_id"],
                    "column_types_used": {"variety.variety_name": "varchar(100)", "variety.variety_id": "int(11)"},
                    "join_hints_used": [],
                },
                ensure_ascii=False,
            )

        class AlwaysSyntaxErrorAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None):
                executed_sql.append(sql)
                raise RuntimeError("(1064, \"You have an error in your SQL syntax\")")

        engine = SQLQueryEngine(mysql_adapter=AlwaysSyntaxErrorAdapter(), llm_text_generator=llm_text_generator)  # type: ignore[arg-type]

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-repair-limit",
                node_id="task-repair-limit:skill_execute",
                metadata={},
            )
        )

        self.assertIsNotNone(result.error)
        self.assertEqual(repair_prompt_count, 1)
        self.assertEqual(len(executed_sql), 2)
        self.assertTrue(any(event.event_type == "skill.sql_repair_failed" for event in result.events))


    async def test_repair_prompt_contains_failed_sql_and_db_error(self) -> None:
        prompts: list[str] = []

        async def llm_text_generator(prompt: str, **_: object) -> str:
            prompts.append(prompt)
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_repair" in prompt:
                return "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'"
            if "当前阶段：sql_generate" in prompt:
                return "SELECT variety_name FROM variety WHERE"
            if '"stage": "result_filtering"' in prompt:
                return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)
            raise AssertionError("unexpected prompt")

        class RepairAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None):
                if sql.endswith("WHERE"):
                    raise RuntimeError(
                        "(1064, \"You have an error in your SQL syntax near WHERE\") "
                        "password=secret guard_pass_token=guard:secret"
                    )
                return fake_query_result(rows=({"variety_name": "龙粳33"},))

        engine = SQLQueryEngine(mysql_adapter=RepairAdapter(), llm_text_generator=llm_text_generator)  # type: ignore[arg-type]

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-repair-prompt",
                node_id="task-repair-prompt:skill_execute",
                metadata={},
            )
        )

        self.assertIsNone(result.error)
        repair_prompts = [prompt for prompt in prompts if "当前阶段：sql_repair" in prompt]
        self.assertEqual(len(repair_prompts), 1)
        repair_prompt = repair_prompts[0]
        self.assertIn("SELECT variety_name FROM variety WHERE", repair_prompt)
        self.assertIn("db_sql_syntax_error", repair_prompt)
        self.assertIn("You have an error in your SQL syntax", repair_prompt)
        self.assertNotIn("password=secret", repair_prompt)
        self.assertNotIn("guard:secret", repair_prompt)

    async def test_repair_does_not_rerun_route_or_schema_prepare(self) -> None:
        prompt_names: list[str] = []

        async def llm_text_generator(prompt: str, **_: object) -> str:
            if "阶段：intent_route" in prompt:
                prompt_names.append("intent_route")
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_repair" in prompt:
                prompt_names.append("sql_repair")
                return "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'"
            if "当前阶段：sql_generate" in prompt:
                prompt_names.append("sql_generate")
                return "SELECT variety_name FROM variety WHERE"
            if '"stage": "result_filtering"' in prompt:
                prompt_names.append("result_filtering")
                return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)
            raise AssertionError("unexpected prompt")

        class RepairAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None):
                if sql.endswith("WHERE"):
                    raise RuntimeError("(1064, \"You have an error in your SQL syntax\")")
                return fake_query_result(rows=({"variety_name": "龙粳33"},))

        engine = SQLQueryEngine(mysql_adapter=RepairAdapter(), llm_text_generator=llm_text_generator)  # type: ignore[arg-type]
        stage_counts = {"intent_route": 0, "schema_context_prepare": 0}

        class CountingCapability:
            def __init__(self, name: str, wrapped):
                self._name = name
                self._wrapped = wrapped

            async def execute(self, request: CapabilityExecutionRequest):
                stage_counts[self._name] += 1
                return await self._wrapped.execute(request)

        engine._capabilities["intent_route"] = CountingCapability(  # type: ignore[index]
            "intent_route",
            engine._capabilities["intent_route"],
        )
        engine._capabilities["schema_context_prepare"] = CountingCapability(  # type: ignore[index]
            "schema_context_prepare",
            engine._capabilities["schema_context_prepare"],
        )

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-repair-no-reroute",
                node_id="task-repair-no-reroute:skill_execute",
                metadata={},
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(stage_counts["intent_route"], 1)
        self.assertEqual(stage_counts["schema_context_prepare"], 1)
        self.assertEqual(prompt_names.count("sql_generate"), 1)
        self.assertEqual(prompt_names.count("sql_repair"), 1)
        self.assertEqual(prompt_names.count("result_filtering"), 1)

    async def test_engine_does_not_repair_guard_security_failures(self) -> None:
        cases = (
            ("write_pattern_detected", "SELECT variety.variety_id FROM variety FOR UPDATE", "variety"),
            ("system_schema_access_denied", "SELECT table_name FROM information_schema.tables", "information_schema"),
            ("table_not_in_route_whitelist", "SELECT id FROM forbidden_table", "forbidden_table"),
        )
        for expected_code, sql, table in cases:
            with self.subTest(expected_code=expected_code):
                async def llm_text_generator(prompt: str, **_: object) -> str:
                    if "阶段：intent_route" in prompt:
                        return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
                    if "当前阶段：sql_repair" in prompt:
                        raise AssertionError("guard security failures must not trigger repair")
                    return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)

                class FakeSQLGenerate:
                    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
                        return CapabilityExecutionResult(
                            capability_id=request.capability_id,
                            task_id=request.task_id,
                            node_id=request.node_id,
                            output_payload={
                                "route_id": "genotype_db",
                                "schema_profile_id": "genotype_profile",
                                "allowed_tables": ["variety"],
                                "selected_tables": ["variety"],
                                "selected_columns": {"variety": ["variety_id"]},
                                "user_question": "查询品种龙粳33的基因型信息",
                                "sql": sql,
                                "tables_used": [table],
                                "columns_used": [f"{table}.variety_id"],
                            },
                        )

                class FailingIfExecutedAdapter:
                    async def execute_readonly(self, sql: str, *, guard_pass_token: str | None):
                        raise AssertionError("guard-blocked SQL must not be executed")

                engine = SQLQueryEngine(mysql_adapter=FailingIfExecutedAdapter(), llm_text_generator=llm_text_generator)  # type: ignore[arg-type]
                engine._capabilities["sql_generate"] = FakeSQLGenerate()  # type: ignore[index]

                result = await engine.execute(
                    SQLQueryEngineRequest(
                        query="查询品种龙粳33的基因型信息",
                        conversation_id="conv-1",
                        task_id=f"task-{expected_code}",
                        node_id=f"task-{expected_code}:skill_execute",
                        metadata={},
                    )
                )

                self.assertIsNotNone(result.error)
                assert result.error is not None
                self.assertEqual(result.error.code, expected_code)
                self.assertFalse(any(event.event_type == "skill.sql_repair_attempted" for event in result.events))



    async def test_repair_clarify_is_treated_as_repair_failure(self) -> None:
        async def llm_text_generator(prompt: str, **_: object) -> str:
            if "阶段：intent_route" in prompt:
                return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)
            if "当前阶段：sql_repair" in prompt:
                return json.dumps(
                    {
                        "mode": "clarify",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "clarifying_question": "请补充查询条件",
                    },
                    ensure_ascii=False,
                )
            if "当前阶段：sql_generate" in prompt:
                return json.dumps(
                    {
                        "mode": "answer",
                        "route_id": "genotype_db",
                        "schema_profile_id": "genotype_profile",
                        "sql": "SELECT variety_name FROM variety WHERE",
                        "tables_used": ["variety"],
                        "columns_used": ["variety.variety_name"],
                        "column_types_used": {"variety.variety_name": "varchar(100)"},
                        "join_hints_used": [],
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"keep_row_indexes": [0]}, ensure_ascii=False)

        class SyntaxErrorAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None):
                raise RuntimeError("(1064, \"You have an error in your SQL syntax\")")

        engine = SQLQueryEngine(mysql_adapter=SyntaxErrorAdapter(), llm_text_generator=llm_text_generator)  # type: ignore[arg-type]

        result = await engine.execute(
            SQLQueryEngineRequest(
                query="查询品种龙粳33的基因型信息",
                conversation_id="conv-1",
                task_id="task-repair-clarify",
                node_id="task-repair-clarify:skill_execute",
                metadata={},
            )
        )

        self.assertIsNone(result.interrupt)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.code, "sql_repair_generation_failed")
        self.assertTrue(any(event.event_type == "skill.sql_repair_failed" for event in result.events))



if __name__ == "__main__":
    unittest.main()
