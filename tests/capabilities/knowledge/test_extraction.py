from __future__ import annotations

import json
import unittest

from src.capabilities.knowledge.extraction import KnowledgeMetadataExtractor, decode_markdown


class RecordingRuntime:
    def __init__(self):
        self.requests = []

    def default_model_edition(self):
        return "configured-main-model"

    def config_snapshot(self):
        return {}

    async def generate_text(self, prompt, **options):
        self.requests.append((prompt, options))
        data = prompt.split("<literature_data>\n", 1)[1].rsplit("\n</literature_data>", 1)[0]
        return json.dumps({"title": "Model title", "summary": "Experimental result " + ("TAIL_DETAIL" if "TAIL_DETAIL" in data else "FIRST_DETAIL"), "authors": [], "year": None, "doi": None, "keywords": []})


class ExtractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_model_json_is_reextracted_once_without_coercion(self):
        runtime = RecordingRuntime()
        generate = runtime.generate_text
        calls = []
        async def flaky(prompt, **options):
            calls.append(prompt)
            if len(calls) == 1:
                return '{"year":"2021","summary":"PRIVATE_BAD_RESPONSE"}'
            return await generate(prompt, **options)
        runtime.generate_text = flaky
        result = await KnowledgeMetadataExtractor(runtime, token_counter=lambda text: len(text))("# Full text")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["title"], "Full text")
        self.assertNotIn("PRIVATE_BAD_RESPONSE", calls[1])

    async def test_every_segment_is_processed_and_frontmatter_has_priority(self):
        runtime = RecordingRuntime()
        extractor = KnowledgeMetadataExtractor(runtime, max_input_tokens=1800, token_counter=lambda text: len(text))
        text = "---\ntitle: Existing title\nauthors: [Known Author]\nyear: 2024\n---\n# Heading\n" + ("Experiment detail.\n" * 260) + "TAIL_DETAIL"
        result = await extractor(text)
        source_requests = [prompt for prompt, _ in runtime.requests if prompt.startswith("Extract metadata")]
        source_parts = [prompt.split("<literature_data>\n", 1)[1].rsplit("\n</literature_data>", 1)[0] for prompt in source_requests]
        self.assertGreater(len(source_parts), 1)
        self.assertEqual("".join(source_parts), text)
        self.assertTrue(all(len(prompt) <= 1800 for prompt, _ in runtime.requests))
        self.assertEqual(result["title"], "Existing title")
        self.assertEqual(result["authors"], ["Known Author"])
        self.assertEqual(result["year"], 2024)
        self.assertIsNone(result["doi"])
        self.assertIn("TAIL_DETAIL", result["summary"])
        self.assertEqual(result["extraction_model"], "configured-main-model")
        self.assertTrue(all(options["model_edition"] == "configured-main-model" for _, options in runtime.requests))

    async def test_provider_or_invalid_structured_output_is_a_failure(self):
        runtime = RecordingRuntime()
        async def broken_counter(_):
            raise RuntimeError("tokenizer unavailable")
        with self.assertRaises(RuntimeError):
            await KnowledgeMetadataExtractor(runtime, token_counter=broken_counter)("# Full text")
        self.assertEqual(runtime.requests, [])
        async def invalid(*args, **kwargs):
            return '{"title": "guessed", "authors": "invented", "summary": "bad"}'
        runtime.generate_text = invalid
        with self.assertRaises(ValueError):
            await KnowledgeMetadataExtractor(runtime, token_counter=lambda text: len(text))("# Full text")

    def test_decode_is_only_a_text_view_not_raw_byte_normalization(self):
        raw = b"\xef\xbb\xbf# A\r\n  \r\n"
        self.assertEqual(decode_markdown(raw), "# A\r\n  \r\n")
        with self.assertRaises(ValueError):
            decode_markdown(b"\x00binary")
        with self.assertRaises(ValueError):
            decode_markdown(b"\xffbad")


if __name__ == "__main__":
    unittest.main()
