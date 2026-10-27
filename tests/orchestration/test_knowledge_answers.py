import unittest

from src.orchestration.agent_loop.knowledge_answers import answer_error, answer_guard
from src.orchestration.agent_loop.models import AgentMessage, AgentModelBinding, AgentModelRequest


class KnowledgeAnswersTest(unittest.TestCase):
    def request(self, sources):
        return AgentModelRequest("sample", AgentModelBinding("model"),
                                 (AgentMessage("system", answer_guard(sources)),), ())

    def source(self, **changes):
        return {"citation": 1, "status": "available", "document_id": "doc", "library_id": "lib",
                "complete": True, "next_cursor": None, "read_characters": 100, "total_characters": 100,
                "metadata": {"title": "Known title", "authors": ["Known Author"], "year": 2021, "doi": "10.1/known"}, **changes}

    def test_citation_must_link_inline_number_to_actual_bibliography(self):
        request = self.request([self.source()])
        valid = "Supported fact [1].\n\n[1] Known Author. Known title. 2021. DOI: 10.1/known."
        self.assertIsNone(answer_error(valid, request))
        for answer in ("No citation", "Known title [1]", "[1] Known Author. Known title. 2021. 10.1/known.",
                       valid.replace("Known title", "Invented title"), valid.replace("[1]", "[2]")):
            with self.subTest(answer=answer):
                self.assertEqual(answer_error(answer, request), "knowledge_answer_citation_invalid")

    def test_incomplete_read_cannot_claim_completion(self):
        request = self.request([self.source(complete=False, next_cursor="continue")])
        answer = "完整阅读后的事实 [1].\n[1] Known Author. Known title. 2021. 10.1/known."
        self.assertEqual(answer_error(answer, request), "knowledge_answer_read_incomplete")

    def test_missing_bibliographic_fields_must_stay_explicitly_unknown(self):
        request = self.request([self.source(metadata={"title": "Manual", "authors": [], "year": None, "doi": None})])
        self.assertIsNone(answer_error("Fact [1].\n[1] Manual; 未提供; 未提供; 未提供", request))
        self.assertEqual(answer_error("Fact [1].\n[1] Manual; Someone; 2026; 10.1/invented", request), "knowledge_answer_citation_invalid")

    def test_no_citation_is_required_for_empty_or_now_unavailable_sources(self):
        self.assertIsNone(answer_error("No evidence in this library", self.request([])))
        request = self.request([self.source(status="forbidden", metadata={})])
        self.assertIsNone(answer_error("权限已撤销，无法读取", request))
        self.assertEqual(answer_error("Fact [1].\n[1] Known title", request), "knowledge_answer_citation_invalid")
