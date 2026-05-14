from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import json
import unittest

from sql_query_skill.intent_route import SQLQueryIntentRouteCapability

from support import make_request


class SQLQueryIntentRouteTest(unittest.TestCase):
    def test_genotype_question_routes_to_genotype_db(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request("skill.sql_query", input_payload={"user_question": "查询某个品种的基因型信息"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["schema_profile_id"], "genotype_profile")

    def test_genotype_intent_keyword_routes_without_llm_call(self) -> None:
        async def semantic_router(_prompt: str) -> str:
            raise AssertionError("intent keyword hit must not call LLM router")

        capability = SQLQueryIntentRouteCapability(semantic_text_generator=semantic_router)
        request = make_request("skill.sql_query", input_payload={"user_question": "查询某个品种的基因型信息"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "intent_keywords")
        self.assertFalse(result.output_payload["llm_router_used"])

    def test_approval_intent_keyword_routes_without_llm_call(self) -> None:
        async def semantic_router(_prompt: str) -> str:
            raise AssertionError("intent keyword hit must not call LLM router")

        capability = SQLQueryIntentRouteCapability(semantic_text_generator=semantic_router)
        request = make_request("skill.sql_query", input_payload={"user_question": "查询近五年审定品种有哪些"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "approval_variety_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "intent_keywords")
        self.assertFalse(result.output_payload["llm_router_used"])

    def test_generic_variety_query_uses_llm_when_no_intent_keyword_matches(self) -> None:
        prompts: list[str] = []

        async def semantic_router(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps({"intent": "database", "route_id": "approval_variety_db"}, ensure_ascii=False)

        capability = SQLQueryIntentRouteCapability(semantic_text_generator=semantic_router)
        request = make_request("skill.sql_query", input_payload={"user_question": "查询龙粳33"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "approval_variety_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "llm_semantic")
        self.assertTrue(result.output_payload["llm_router_used"])
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("variety_overview", prompts[0])

    def test_generic_variety_query_without_llm_requires_route_clarification(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request("skill.sql_query", input_payload={"user_question": "查询龙粳33"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, "route_not_resolved")
        self.assertEqual(
            result.interrupt.required_fields,
            {"route_id": {"options": ["approval_variety_db", "genotype_db"]}},
        )

    def test_explicit_approval_database_display_name_wins_over_broad_lookup(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "skill.sql_query",
            input_payload={"user_question": "那么龙粳18呢？补充信息：route_id=审定品种库吧"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "approval_variety_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "explicit_route_alias")

    def test_approval_database_common_reversed_name_is_an_explicit_alias(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "skill.sql_query",
            input_payload={"user_question": "龙粳18的详细信息\n补充信息：route_id=品种审定库，要他的所有信息。"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "approval_variety_db")
        self.assertEqual(result.output_payload["inferred_crop"], "rice")

    def test_explicit_genotype_database_display_name_wins_over_generic_variety_word(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "skill.sql_query",
            input_payload={"user_question": "查询品种龙粳33，route_id=基因型数据库"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "explicit_route_alias")

    def test_approval_route_without_crop_uses_broad_approval_query(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request("skill.sql_query", input_payload={"user_question": "查询近五年审定品种有哪些"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "approval_variety_db")
        self.assertIsNone(result.output_payload["inferred_crop"])
        self.assertEqual(result.output_payload["route_resolution_strategy"], "intent_keywords")
        self.assertTrue(result.output_payload["no_crop_broad_query"])
        self.assertEqual(
            set(result.output_payload["allowed_tables"]),
            {"corn_varieties", "rice_varieties", "cotton_varieties", "wheat_varieties", "soybean_varieties"},
        )
        self.assertTrue(result.output_payload["candidate_routes"])

    def test_composite_approval_and_genotype_question_marks_decomposition(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "skill.sql_query",
            input_payload={"user_question": "龙粳33的审定信息和基因型信息都查一下"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, "route_not_resolved")

    def test_variety_info_and_gene_info_question_marks_decomposition(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "skill.sql_query",
            input_payload={"user_question": "你给我查一下龙粳33的品种信息和基因信息"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, "route_not_resolved")

    def test_route_hint_can_select_public_macro_subtask_route(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "skill.sql_query",
            input_payload={
                "user_question": "查询龙粳33的基因型信息",
                "route_hint": "genotype_db",
                "subtask_label": "基因型信息",
                "parent_question": "龙粳33的审定信息和基因型信息都查一下",
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "route_hint")
        self.assertEqual(result.output_payload["subtask_label"], "基因型信息")
        self.assertEqual(result.output_payload["parent_question"], "龙粳33的审定信息和基因型信息都查一下")

    def test_valid_llm_semantic_router_can_select_supported_route(self) -> None:
        prompts: list[str] = []

        async def semantic_router(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)

        capability = SQLQueryIntentRouteCapability(semantic_text_generator=semantic_router)
        request = make_request("skill.sql_query", input_payload={"user_question": "帮我看看这个材料的分型信息"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "llm_semantic")
        self.assertTrue(result.output_payload["llm_router_used"])
        self.assertIn("route_id", prompts[0])

    def test_semantic_router_receives_capability_request_when_supported(self) -> None:
        seen_metadata: list[dict] = []

        async def semantic_router(prompt: str, *, request) -> str:
            seen_metadata.append(dict(request.metadata))
            return json.dumps({"intent": "database", "route_id": "genotype_db"}, ensure_ascii=False)

        capability = SQLQueryIntentRouteCapability(semantic_text_generator=semantic_router)
        request = make_request(
            "skill.sql_query",
            input_payload={"user_question": "帮我看看这个材料的分型信息"},
            metadata={"deep_thinking": True, "main_agent_reasoning_effort": "medium"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(
            seen_metadata,
            [{"deep_thinking": False, "main_agent_reasoning_effort": "medium", "main_agent_thinking_enabled": False}],
        )

    def test_invalid_llm_semantic_router_output_falls_back_to_deterministic_route(self) -> None:
        def semantic_router(_prompt: str) -> str:
            return json.dumps({"intent": "database", "route_id": "evil_db"}, ensure_ascii=False)

        capability = SQLQueryIntentRouteCapability(semantic_text_generator=semantic_router)
        request = make_request("skill.sql_query", input_payload={"user_question": "查询某个品种的基因型信息"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "intent_keywords")
        self.assertFalse(result.output_payload["llm_router_used"])
