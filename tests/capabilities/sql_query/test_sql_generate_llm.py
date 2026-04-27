from __future__ import annotations

import asyncio
import json
import unittest

from src.capabilities.sql_query.sql_generate import SQLQuerySQLGenerateCapability

from tests.capabilities.sql_query.support import make_request


SCHEMA_CONTEXT = {
    "route_id": "genotype_db",
    "schema_profile_id": "genotype_profile",
    "sql_policy_profile": "strict_readonly_mysql",
    "allowed_tables": ["variety"],
    "selected_tables": ["variety"],
    "selected_columns": {"variety": ["variety_id", "variety_name"]},
    "selected_column_details": {
        "variety": [
            {"name": "variety_id", "sql_type": "int(11)", "description": "自增ID"},
            {"name": "variety_name", "sql_type": "varchar(100)", "description": "品种名称"},
        ]
    },
    "join_hints": [],
    "context_summary": "variety 表",
    "user_question": "查询品种龙粳33的基因型信息",
}


def request_for_sql_generate() -> object:
    return make_request(
        "sql_query.sql_generate",
        dependency_outputs={"schema": SCHEMA_CONTEXT},
    )


class SQLQuerySQLGenerateLLMTest(unittest.TestCase):
    def test_uses_llm_answer_when_structured_output_is_valid(self) -> None:
        async def llm_text_generator(prompt: str) -> str:
            self.assertIn("genotype_db", prompt)
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": "SELECT variety_name FROM variety LIMIT 20",
                    "tables_used": ["variety"],
                    "columns_used": ["variety.variety_name"],
                    "column_types_used": {"variety.variety_name": "varchar(100)"},
                    "join_hints_used": [],
                }
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["sql"], "SELECT variety_name FROM variety LIMIT 20")
        self.assertEqual(result.output_payload["generation_source"], "llm")
        self.assertEqual(result.output_payload["llm_mode"], "answer")
        self.assertFalse(result.output_payload["fallback_used"])
        self.assertEqual(result.output_payload["tables_used"], ["variety"])
        self.assertEqual(result.output_payload["column_types_used"], {"variety.variety_name": "varchar(100)"})
        self.assertTrue(any(event.event_type == "sql_query.llm_call" for event in result.events))


    def test_invalid_llm_column_name_falls_back_to_heuristic_generator(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": "SELECT fake_column FROM variety LIMIT 20",
                    "tables_used": ["variety"],
                    "columns_used": ["variety.fake_column"],
                    "column_types_used": {"variety.fake_column": "varchar(100)"},
                }
            )

        capability = SQLQuerySQLGenerateCapability(
            llm_text_generator=llm_text_generator,
            generator=lambda _: "SELECT variety_name FROM variety LIMIT 50",
        )

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "validation_failed")

    def test_invalid_llm_column_type_falls_back_to_heuristic_generator(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": "SELECT variety_name FROM variety LIMIT 20",
                    "tables_used": ["variety"],
                    "columns_used": ["variety.variety_name"],
                    "column_types_used": {"variety.variety_name": "text"},
                }
            )

        capability = SQLQuerySQLGenerateCapability(
            llm_text_generator=llm_text_generator,
            generator=lambda _: "SELECT variety_name FROM variety LIMIT 50",
        )

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "validation_failed")

    def test_invalid_llm_output_falls_back_to_heuristic_generator(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return "not json"

        capability = SQLQuerySQLGenerateCapability(
            llm_text_generator=llm_text_generator,
            generator=lambda _: "SELECT variety_name FROM variety LIMIT 50",
        )

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["sql"], "SELECT variety_name FROM variety LIMIT 50")
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "parse_failed")
        self.assertTrue(result.output_payload["fallback_used"])
        self.assertIn("fallback_reason", result.output_payload)
        self.assertTrue(any(event.event_type == "sql_query.llm_fallback" for event in result.events))

    def test_llm_clarify_returns_interrupt_without_sql(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "clarify",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "missing_info": ["variety_name"],
                    "clarifying_question": "请补充要查询的品种名称。",
                }
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.question, "请补充要查询的品种名称。")
        self.assertEqual(result.interrupt.reason_code, "llm_clarification_required")
        self.assertNotIn("sql", result.output_payload)
        self.assertEqual(result.output_payload["llm_mode"], "clarify")

    def test_llm_reject_returns_non_retriable_error(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "reject",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "reject_reason": "问题超出当前 SQLQuery 支持范围。",
                    "supported_scope_hint": "可查询品种、基因型、QTN 等信息。",
                }
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "llm_rejected_request")
        self.assertFalse(result.error.retriable)
        self.assertEqual(result.output_payload["llm_mode"], "reject")
        self.assertNotIn("sql", result.output_payload)


if __name__ == "__main__":
    unittest.main()
