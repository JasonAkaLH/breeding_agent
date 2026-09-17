from __future__ import annotations

import ssl
from dataclasses import dataclass

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream, SOCKET_OPTION

from .endpoint_policy import EndpointPolicy, ValidatedEndpoint


class _PinnedNetworkBackend(AsyncNetworkBackend):
    def __init__(self, endpoint: ValidatedEndpoint, policy: EndpointPolicy) -> None:
        self._endpoint = endpoint
        self._policy = policy
        self._backend = AutoBackend()
        self._ordinal = 0

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: list[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        if host.rstrip(".").lower() != self._endpoint.hostname or port != self._endpoint.port:
            raise OSError("MCP policy-bound connector rejected an unexpected origin")
        addresses = self._endpoint.connect_ips
        if not addresses:
            raise OSError("MCP policy-bound connector has no validated address")
        address = addresses[self._ordinal % len(addresses)]
        self._ordinal += 1
        self._policy.validate_connection_ip(self._endpoint, address)
        return await self._backend.connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: list[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        del path, timeout, socket_options
        raise OSError("Unix sockets are not supported by the MCP Gateway")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, endpoint: ValidatedEndpoint, policy: EndpointPolicy) -> None:
        super().__init__(verify=True, trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            retries=0,
            network_backend=_PinnedNetworkBackend(endpoint, policy),
        )


@dataclass(frozen=True, slots=True)
class PolicyBoundHTTPConnection:
    endpoint_url: str
    client: httpx.AsyncClient


def build_policy_bound_http_connection(
    policy: EndpointPolicy,
    endpoint: ValidatedEndpoint,
) -> PolicyBoundHTTPConnection:
    client = httpx.AsyncClient(
        transport=_PinnedAsyncHTTPTransport(endpoint, policy),
        follow_redirects=False,
        trust_env=False,
    )
    return PolicyBoundHTTPConnection(endpoint.normalized_url, client)
