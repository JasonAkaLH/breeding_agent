from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

import yaml

from tests.api.support import APITestCase


class AuthLoginRedirectAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()
        self.embed_config_path = self.workspace / "login_embed_origins.yaml"
        self.write_embed_origins([])
        config_path = patch("src.api.routes.auth._LOGIN_EMBED_ORIGINS_PATH", self.embed_config_path)
        config_path.start()
        self.addCleanup(config_path.stop)

    def write_embed_origins(self, origins: object) -> None:
        self.embed_config_path.write_text(yaml.safe_dump({"allowed_origins": origins}), encoding="utf-8")

    async def test_browser_login_issues_a_current_token_and_disables_caching(self) -> None:
        response = await self.client.get("/api/v1/auth/login-redirect", params={"username": " alice "})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn("location", response.headers)
        nonce = re.search(r'<script nonce="([\w-]+)">', response.text)[1]
        self.assertIn(f"script-src 'nonce-{nonce}'", response.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        token = re.search(r'"(maf_tok_[\w-]+)"', response.text)[1]
        me = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["username"], "alice")

    async def test_iframe_login_allows_only_configured_parent_origins(self) -> None:
        self.write_embed_origins(["https://portal.example/", "http://localhost:18581", "https://portal.example"])
        response = await self.client.get("/api/v1/auth/login-redirect", params={"username": "alice"})
        self.assertEqual(response.status_code, 200, response.text)
        policy = response.headers["content-security-policy"]
        self.assertIn("frame-ancestors https://portal.example http://localhost:18581", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("script-src 'nonce-", policy)
        self.assertNotIn("x-frame-options", response.headers)
        self.assertNotIn("window.top", response.text)
        self.assertNotIn("window.parent", response.text)

    async def test_invalid_embed_origin_configuration_does_not_issue_a_token(self) -> None:
        previous = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        for origin in (
            "*", "https://*.example", "'self'", "https://portal.example/path",
            "https://user:password@portal.example", "https://portal.example?query=x",
            "https://portal.example#fragment", "javascript:alert(1)",
            "https://portal.example;script-src *", "https://portal.example\nhttps://other.example",
            "https://portal.example:invalid", "https://", "https://portal.example\\evil",
        ):
            with self.subTest(origin=origin):
                self.write_embed_origins([origin])
                response = await self.client.get("/api/v1/auth/login-redirect", params={"username": "alice"})
                self.assertEqual(response.status_code, 503, response.text)
                self.assertNotIn(origin, response.text)
        me = await self.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {previous.json()['access_token']}"},
        )
        self.assertEqual(me.status_code, 200)

    async def test_yaml_edits_apply_on_next_login_and_override_the_removed_env_setting(self) -> None:
        with patch.dict("os.environ", {"MAF_LOGIN_EMBED_ALLOWED_ORIGINS": "https://old-env.example"}):
            for origins, expected in (
                (["https://first.example"], "https://first.example"),
                (["https://second.example"], "https://second.example"),
                ([], "'none'"),
            ):
                self.write_embed_origins(origins)
                response = await self.client.get("/api/v1/auth/login-redirect", params={"username": "alice"})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-security-policy"].endswith(f"frame-ancestors {expected}"))
                self.assertNotIn("old-env.example", response.headers["content-security-policy"])

    async def test_missing_malformed_or_invalid_yaml_does_not_replace_the_current_token(self) -> None:
        previous = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        invalid_documents = (
            None, "", "allowed_origins: [", "allowed_origins: *undefined",
            "{}", "[]", "allowed_origins: null", "allowed_origins: https://portal.example",
            "allowed_origins: [123]", "allowed_origins: [true]", "allowed_origins: ['']",
            "allowed_origins: []\nunknown: true",
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                if document is None:
                    self.embed_config_path.unlink()
                else:
                    self.embed_config_path.write_text(document, encoding="utf-8")
                response = await self.client.get("/api/v1/auth/login-redirect", params={"username": "alice"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"], "Invalid login embed origin configuration")
        me = await self.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {previous.json()['access_token']}"},
        )
        self.assertEqual(me.status_code, 200)

    async def test_browser_relogin_invalidates_the_previous_token(self) -> None:
        previous = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        response = await self.client.get("/api/v1/auth/login-redirect", params={"username": "alice"})
        self.assertEqual(response.status_code, 200)
        me = await self.client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {previous.json()['access_token']}"},
        )
        self.assertEqual(me.status_code, 401)

    async def test_invalid_usernames_do_not_replace_an_existing_login(self) -> None:
        previous = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        for username in ("", "ab", "a" * 65, "alice/bob", "<script>alert(1)</script>"):
            with self.subTest(username=username):
                response = await self.client.get("/api/v1/auth/login-redirect", params={"username": username})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["detail"]["code"], "invalid_username")
        self.assertEqual((await self.client.get("/api/v1/auth/login-redirect")).status_code, 422)
        me = await self.client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {previous.json()['access_token']}"},
        )
        self.assertEqual(me.status_code, 200)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the browser handoff")
    async def test_browser_handoff_uses_the_frontend_storage_key_and_same_origin_app_root(self) -> None:
        response = await self.client.get("/api/v1/auth/login-redirect", params={"username": "alice"})
        self.assertEqual(response.status_code, 200)
        frontend = Path("frontend/src/App.tsx").read_text()
        storage_key = re.search(r"const AUTH_TOKEN_STORAGE_KEY = '([^']+)'", frontend)[1]
        for prefix in ("/", "/seedpilot/", "/nested/seedpilot/"):
            with self.subTest(prefix=prefix):
                browser = self._execute_handoff(
                    response.text,
                    f"https://chat.example{prefix}api/v1/auth/login-redirect?username=alice&redirect=https://evil.example/#ignored",
                )
                self.assertEqual(browser["redirect"], f"https://chat.example{prefix}")
                self.assertEqual(list(browser["storage"]), [storage_key])
                token = browser["storage"][storage_key]
                me = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(me.status_code, 200)
                self.assertEqual(me.json()["user"]["username"], "alice")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the browser handoff")
    async def test_storage_failure_is_visible_and_does_not_redirect(self) -> None:
        response = await self.client.get("/api/v1/auth/login-redirect", params={"username": "alice"})
        self.assertEqual(response.status_code, 200)
        browser = self._execute_handoff(
            response.text, "https://chat.example/seedpilot/api/v1/auth/login-redirect?username=alice", fail_storage=True,
        )
        self.assertIsNone(browser["redirect"])
        self.assertEqual(browser["storage"], {})
        self.assertIn("无法保存登录状态", browser["message"])

    def _execute_handoff(self, html: str, url: str, *, fail_storage: bool = False) -> dict:
        script = re.search(r'<script nonce="[\w-]+">(.*?)</script>', html, re.S)[1]
        result = subprocess.run(
            ["node", "-e", """
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const storage = {};
const status = { textContent: '' };
let redirect = null;
vm.runInNewContext(input.script, {
  URL,
  window: {
    localStorage: { setItem(key, value) {
      if (input.fail_storage) throw new Error('Storage disabled');
      storage[key] = value;
    } },
    location: { href: input.url, replace(url) { redirect = String(url); } },
  },
  document: { getElementById(id) {
    if (id !== 'login-status') throw new Error('Unknown status element');
    return status;
  } },
});
process.stdout.write(JSON.stringify({ storage, redirect, message: status.textContent }));
"""],
            input=json.dumps({"script": script, "url": url, "fail_storage": fail_storage}),
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)
