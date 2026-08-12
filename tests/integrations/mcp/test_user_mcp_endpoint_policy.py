from __future__ import annotations

import asyncio
import unittest

from src.integrations.mcp.endpoint_policy import (
    EndpointAllowlist,
    EndpointPolicy,
    EndpointPolicyError,
    IPClassification,
    classify_ip,
)
from src.integrations.mcp.headers import HeaderPolicyError, validate_static_headers
from src.integrations.mcp.policy_connection import (
    _PinnedNetworkBackend,
    build_policy_bound_http_connection,
)


class _Resolver:
    def __init__(self, values):
        self.values = values

    def resolve(self, hostname: str, port: int):
        value = self.values[hostname]
        return value() if callable(value) else value


class UserMCPEndpointPolicyTests(unittest.TestCase):
    def test_public_https_is_normalized_and_dns_bound(self) -> None:
        # Documentation ranges are non-global in ipaddress, so use a real global classification fixture.
        policy = EndpointPolicy(resolver=_Resolver({"mcp.example.com": ["8.8.8.8"]}))
        endpoint = policy.validate("HTTPS://MCP.Example.COM:443/rpc")

        self.assertEqual(endpoint.normalized_url, "https://mcp.example.com/rpc")
        self.assertEqual(endpoint.origin, "https://mcp.example.com:443")
        self.assertEqual(endpoint.connect_ips, ("8.8.8.8",))
        self.assertEqual(endpoint.server_hostname, "mcp.example.com")
        policy.validate_connection_ip(endpoint, "8.8.8.8")
        with self.assertRaisesRegex(EndpointPolicyError, "dns_rebinding"):
            policy.validate_connection_ip(endpoint, "1.1.1.1")

    def test_dangerous_address_in_any_answer_rejects_all(self) -> None:
        policy = EndpointPolicy(resolver=_Resolver({"mcp.example.com": ["8.8.8.8", "127.0.0.1"]}))
        with self.assertRaisesRegex(EndpointPolicyError, "ip_forbidden"):
            policy.validate("https://mcp.example.com/rpc")

    def test_private_https_and_http_require_admin_allowlist(self) -> None:
        resolver = _Resolver({"mcp.corp.example": ["10.2.3.4"]})
        with self.assertRaisesRegex(EndpointPolicyError, "private_not_allowlisted"):
            EndpointPolicy(resolver=resolver).validate("https://mcp.corp.example/rpc")

        allowlist = EndpointAllowlist.from_values(domains=["corp.example"], cidrs=["10.0.0.0/8"])
        policy = EndpointPolicy(resolver=resolver, allowlist=allowlist)
        self.assertFalse(policy.validate("https://mcp.corp.example/rpc").plaintext_http)
        self.assertTrue(policy.validate("http://mcp.corp.example/rpc").plaintext_http)

    def test_metadata_and_local_classes_cannot_be_allowlisted(self) -> None:
        allowlist = EndpointAllowlist.from_values(cidrs=["0.0.0.0/0", "::/0"])
        for address, expected in (
            ("169.254.169.254", IPClassification.METADATA),
            ("::ffff:169.254.169.254", IPClassification.METADATA),
            ("127.0.0.1", IPClassification.LOOPBACK),
            ("::ffff:127.0.0.1", IPClassification.LOOPBACK),
            ("fe80::1", IPClassification.LINK_LOCAL),
        ):
            self.assertEqual(classify_ip(address), expected)
            with self.assertRaisesRegex(EndpointPolicyError, "ip_forbidden"):
                EndpointPolicy(allowlist=allowlist).validate(f"https://[{address}]/rpc" if ":" in address else f"https://{address}/rpc")

    def test_redirect_and_resolution_change_fail_closed(self) -> None:
        answers = [["8.8.8.8"], ["1.1.1.1"]]
        resolver = _Resolver({"mcp.example.com": lambda: answers.pop(0)})
        policy = EndpointPolicy(resolver=resolver)
        endpoint = policy.validate("https://mcp.example.com/rpc")
        with self.assertRaisesRegex(EndpointPolicyError, "dns_rebinding"):
            policy.revalidate(endpoint)

        policy = EndpointPolicy(resolver=_Resolver({"mcp.example.com": ["8.8.8.8"], "other.example.com": ["1.1.1.1"]}))
        source = policy.validate("https://mcp.example.com/rpc")
        with self.assertRaisesRegex(EndpointPolicyError, "cross_origin"):
            policy.validate_redirect(source, "https://other.example.com/rpc", status_code=307)
        redirected = policy.validate_redirect(source, "https://mcp.example.com/next", status_code=308)
        self.assertEqual(redirected.origin, source.origin)

    def test_header_values_are_separate_and_protected_names_rejected(self) -> None:
        validated = validate_static_headers({"X-Api-Key": "canary", "X-Tenant": "alpha"})
        self.assertEqual(validated.names, ("x-api-key", "x-tenant"))
        self.assertNotIn("canary", repr(validated.credential_values))
        self.assertEqual(validated.credential_values.reveal()["x-api-key"], "canary")
        for name in ("Host", "Authorization", "Cookie", "MCP-Protocol-Version", "Mcp-Param-foo"):
            with self.assertRaisesRegex(HeaderPolicyError, "protected"):
                validate_static_headers({name: "value"})

    def test_connector_dials_validated_ip_and_never_auto_follows_redirects(self) -> None:
        policy = EndpointPolicy(
            resolver=_Resolver({"mcp.example.com": ["8.8.8.8"]})
        )
        endpoint = policy.validate("https://mcp.example.com/rpc")

        class FakeBackend:
            def __init__(self) -> None:
                self.calls = []

            async def connect_tcp(self, host, port, **kwargs):
                self.calls.append((host, port, kwargs))
                return object()

        pinned = _PinnedNetworkBackend(endpoint, policy)
        fake = FakeBackend()
        pinned._backend = fake
        asyncio.run(pinned.connect_tcp("mcp.example.com", 443))
        self.assertEqual(fake.calls[0][:2], ("8.8.8.8", 443))
        with self.assertRaisesRegex(OSError, "unexpected origin"):
            asyncio.run(pinned.connect_tcp("other.example.com", 443))

        connection = build_policy_bound_http_connection(policy, endpoint)
        self.assertFalse(connection.client.follow_redirects)
        asyncio.run(connection.client.aclose())


if __name__ == "__main__":
    unittest.main()
