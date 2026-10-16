from __future__ import annotations

import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest

from fastapi.routing import APIRoute
from pydantic import TypeAdapter

from src.api.app import create_app
from src.api.dto import UserMCPDeletePendingResponse, MCPBusinessResultView
from src.integrations.mcp.gateway_models import MCPContinueStatus, MCPCancelStatus
from scripts.update_api_doc_contract import updated_document
from tests.api.support import APITestCase

PAGE = Path('docs/api/api-doc.html')


def embedded(source: str, identifier: str):
    match = re.search(rf'<script[^>]*id="{identifier}"[^>]*>(.*?)</script>', source, re.S)
    if match is None:
        raise AssertionError(f'Missing {identifier}')
    return json.loads(match[1])


class GuideParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.anchors: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if 'id' in attributes:
            self.ids.append(attributes['id'])
        if tag == 'a' and attributes.get('href', '').startswith('#'):
            self.anchors.append(attributes['href'][1:])


class DeveloperDocsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PAGE.read_text()
        cls.contract = embedded(cls.source, 'api-contract')
        cls.entries = embedded(cls.source, 'endpoint-data')
        cls.app = create_app(runtime=object())
        cls.routes = {
            (method, route.path): route
            for route in cls.app.routes if isinstance(route, APIRoute) and route.include_in_schema
            for method in route.methods
        }

    def test_embedded_contract_and_example_match_current_source(self):
        self.assertEqual(self.contract, self.app.openapi())
        self.assertEqual(self.source, updated_document(self.source))
        example = re.search(r'<code id="frontend-example">(.*?)</code>', self.source, re.S)
        self.assertIsNotNone(example)
        self.assertEqual(html.unescape(example[1]), Path('docs/api/frontend-example.ts').read_text())

    def test_reference_covers_every_method_and_path_exactly_once(self):
        documented = [(item['method'], item['path']) for item in self.entries]
        self.assertEqual(len(documented), len(set(documented)))
        self.assertEqual(set(documented), set(self.routes))
        for item in self.entries:
            self.assertTrue(item['title'] and item['when'] and item['notes'])
            route = self.routes[(item['method'], item['path'])]
            if item['path'] == '/api/v1/mcp/servers/{server_id}' and item['method'] == 'DELETE':
                self.assertEqual(item['success'], ['202', '204'])
            else:
                self.assertIn(str(route.status_code or 200), item['success'])

    def test_all_json_examples_validate_against_real_dtos(self):
        view = re.search(r'<code id="mcp-result-example">(.*?)</code>', self.source, re.S)
        MCPBusinessResultView.model_validate(json.loads(html.unescape(view[1])))
        for item in self.entries:
            with self.subTest(method=item['method'], path=item['path']):
                route = self.routes[(item['method'], item['path'])]
                if item['body'] is not None:
                    TypeAdapter(route.body_field.field_info.annotation).validate_python(item['body'])
                elif route.body_field is not None:
                    operation = self.app.openapi()['paths'][route.path][item['method'].lower()]
                    self.assertIn('multipart/form-data', operation['requestBody']['content'])
                if item['response'] is not None:
                    response_model = route.response_model
                    if item['method'] == 'DELETE' and item['path'] == '/api/v1/mcp/servers/{server_id}':
                        response_model = UserMCPDeletePendingResponse
                    if response_model is not None:
                        TypeAdapter(response_model).validate_python(item['response'])
                    else:
                        # Knowledge endpoints currently expose JSON records without
                        # a response DTO; these illustrative records claim no schema.
                        self.assertEqual(item['group'], 'knowledge')
                        self.assertIsInstance(item['response'], dict)
                        json.dumps(item['response'])
                if '/mcp-calls/' in item['path']:
                    enum = MCPContinueStatus if item['path'].endswith('/continue') else MCPCancelStatus
                    self.assertIn(item['response']['status'], {status.value for status in enum})

    def test_authentication_is_explicit_despite_missing_openapi_security_metadata(self):
        public = {(item['method'], item['path']) for item in self.entries if item['public']}
        self.assertEqual(public, {('POST', '/api/v1/auth/login'), ('GET', '/api/v1/capabilities')})
        self.assertIn('Authorization: Bearer', self.source)

    def test_static_anchors_and_frontend_guide_sections_are_complete(self):
        parser = GuideParser()
        parser.feed(self.source)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertTrue(set(parser.anchors).issubset(set(parser.ids)))
        for section in ('quickstart', 'conventions', 'chat', 'streaming', 'interrupts', 'files', 'mcp', 'knowledge', 'reference', 'schemas', 'checklist'):
            self.assertIn(section, parser.ids)


class DeveloperDocsAPITest(APITestCase):
    async def test_api_doc_serves_complete_guide_without_authentication(self):
        await self.logout()
        response = await self.client.get('/api-doc')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response.headers['content-type'])
        self.assertEqual(response.text, PAGE.read_text())
        openapi = await self.client.get('/openapi.json')
        self.assertEqual(openapi.status_code, 200)
        self.assertEqual(embedded(response.text, 'api-contract'), openapi.json())

    async def test_changelog_route_and_alias_keep_existing_content(self):
        await self.logout()
        response = await self.client.get('/api-doc/API更新日志.md')
        alias = await self.client.get('/api-doc/api-changelog.md')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/markdown', response.headers['content-type'])
        self.assertEqual(response.text, Path('docs/api/API更新日志.md').read_text())
        self.assertEqual(alias.status_code, 200)
        self.assertEqual(alias.text, response.text)
