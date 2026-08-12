from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit


MAX_ENDPOINT_URL_BYTES = 2048
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class EndpointPolicyError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IPClassification(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    LOOPBACK = "loopback"
    LINK_LOCAL = "link_local"
    MULTICAST = "multicast"
    RESERVED = "reserved"
    UNSPECIFIED = "unspecified"
    METADATA = "metadata"


class EndpointResolver(Protocol):
    def resolve(self, hostname: str, port: int) -> Iterable[str]: ...


class SocketEndpointResolver:
    def resolve(self, hostname: str, port: int) -> Iterable[str]:
        try:
            return tuple(item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            raise EndpointPolicyError("mcp_endpoint_dns_failed") from exc


@dataclass(frozen=True, slots=True)
class EndpointAllowlist:
    domains: tuple[str, ...] = ()
    cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_values(
        cls,
        *,
        domains: Sequence[str] = (),
        cidrs: Sequence[str] = (),
    ) -> "EndpointAllowlist":
        normalized_domains = tuple(_normalize_allowlist_domain(value) for value in domains)
        try:
            networks = tuple(ipaddress.ip_network(value, strict=True) for value in cidrs)
        except ValueError as exc:
            raise EndpointPolicyError("mcp_endpoint_allowlist_invalid") from exc
        return cls(domains=normalized_domains, cidrs=networks)

    def permits_domain(self, hostname: str) -> bool:
        return any(hostname == domain or hostname.endswith(f".{domain}") for domain in self.domains)

    def permits_ip(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(address.version == network.version and address in network for network in self.cidrs)


@dataclass(frozen=True, slots=True)
class ValidatedEndpoint:
    normalized_url: str
    scheme: str
    hostname: str
    port: int
    origin: str
    resolved_ips: tuple[str, ...]
    allowed_ips: tuple[str, ...]
    plaintext_http: bool

    @property
    def connect_ips(self) -> tuple[str, ...]:
        return self.allowed_ips

    @property
    def server_hostname(self) -> str:
        return self.hostname

    @property
    def host_header(self) -> str:
        default_port = 443 if self.scheme == "https" else 80
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        return host if self.port == default_port else f"{host}:{self.port}"

    def permits_connection_ip(self, address: str) -> bool:
        try:
            normalized = str(ipaddress.ip_address(address))
        except ValueError:
            return False
        return normalized in self.allowed_ips


class EndpointPolicy:
    def __init__(
        self,
        *,
        resolver: EndpointResolver | None = None,
        allowlist: EndpointAllowlist | None = None,
    ) -> None:
        self._resolver = resolver or SocketEndpointResolver()
        self._allowlist = allowlist or EndpointAllowlist()

    def validate(self, url: str) -> ValidatedEndpoint:
        parsed, normalized_url, hostname, port = _normalize_url(url)
        try:
            direct_ip = ipaddress.ip_address(hostname)
            addresses = (direct_ip,)
        except ValueError:
            try:
                addresses = tuple(
                    sorted(
                        {ipaddress.ip_address(value) for value in self._resolver.resolve(hostname, port)},
                        key=lambda item: (item.version, int(item)),
                    )
                )
            except EndpointPolicyError:
                raise
            except (TypeError, ValueError, OSError) as exc:
                raise EndpointPolicyError("mcp_endpoint_dns_failed") from exc
        if not addresses:
            raise EndpointPolicyError("mcp_endpoint_dns_failed")

        domain_allowed = self._allowlist.permits_domain(hostname)
        plaintext = parsed.scheme == "http"
        for address in addresses:
            category = classify_ip(address)
            if category in {
                IPClassification.METADATA,
                IPClassification.LOOPBACK,
                IPClassification.LINK_LOCAL,
                IPClassification.MULTICAST,
                IPClassification.RESERVED,
                IPClassification.UNSPECIFIED,
            }:
                raise EndpointPolicyError("mcp_endpoint_ip_forbidden")
            if category is IPClassification.PRIVATE and not (domain_allowed or self._allowlist.permits_ip(address)):
                raise EndpointPolicyError("mcp_endpoint_private_not_allowlisted")
            if plaintext and not (domain_allowed or self._allowlist.permits_ip(address)):
                raise EndpointPolicyError("mcp_endpoint_http_not_allowlisted")

        resolved = tuple(str(address) for address in addresses)
        return ValidatedEndpoint(
            normalized_url=normalized_url,
            scheme=parsed.scheme,
            hostname=hostname,
            port=port,
            origin=_origin(parsed.scheme, hostname, port),
            resolved_ips=resolved,
            allowed_ips=resolved,
            plaintext_http=plaintext,
        )

    def validate_connection_ip(self, endpoint: ValidatedEndpoint, address: str) -> None:
        if not endpoint.permits_connection_ip(address):
            raise EndpointPolicyError("mcp_endpoint_dns_rebinding")

    def revalidate(self, endpoint: ValidatedEndpoint) -> ValidatedEndpoint:
        current = self.validate(endpoint.normalized_url)
        if current.resolved_ips != endpoint.resolved_ips:
            raise EndpointPolicyError("mcp_endpoint_dns_rebinding")
        return current

    def validate_redirect(self, source: ValidatedEndpoint, target_url: str, *, status_code: int) -> ValidatedEndpoint:
        if status_code not in {307, 308}:
            raise EndpointPolicyError("mcp_endpoint_redirect_forbidden")
        target = self.validate(target_url)
        if source.scheme == "https" and target.scheme != "https":
            raise EndpointPolicyError("mcp_endpoint_redirect_downgrade")
        if target.origin != source.origin:
            raise EndpointPolicyError("mcp_endpoint_redirect_cross_origin")
        return target


def classify_ip(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> IPClassification:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise EndpointPolicyError("mcp_endpoint_ip_invalid") from exc
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return classify_ip(parsed.ipv4_mapped)
    if parsed in _METADATA_IPS:
        return IPClassification.METADATA
    if parsed.is_loopback:
        return IPClassification.LOOPBACK
    if parsed.is_link_local:
        return IPClassification.LINK_LOCAL
    if parsed.is_multicast:
        return IPClassification.MULTICAST
    if parsed.is_unspecified:
        return IPClassification.UNSPECIFIED
    if parsed.is_private:
        return IPClassification.PRIVATE
    if parsed.is_reserved or not parsed.is_global:
        return IPClassification.RESERVED
    return IPClassification.PUBLIC


def _normalize_url(url: str) -> tuple[SplitResult, str, str, int]:
    if not isinstance(url, str) or not url or len(url.encode("utf-8")) > MAX_ENDPOINT_URL_BYTES:
        raise EndpointPolicyError("mcp_endpoint_url_invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise EndpointPolicyError("mcp_endpoint_url_invalid") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise EndpointPolicyError("mcp_endpoint_url_invalid")
    if parsed.fragment:
        raise EndpointPolicyError("mcp_endpoint_url_invalid")
    hostname = _normalize_hostname(parsed.hostname)
    port = port or (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise EndpointPolicyError("mcp_endpoint_url_invalid")
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"
    path = parsed.path or "/"
    normalized = urlunsplit((scheme, netloc, path, parsed.query, ""))
    normalized_parsed = urlsplit(normalized)
    return normalized_parsed, normalized, hostname, port


def _normalize_hostname(hostname: str) -> str:
    value = hostname.rstrip(".").lower()
    if not value:
        raise EndpointPolicyError("mcp_endpoint_url_invalid")
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise EndpointPolicyError("mcp_endpoint_url_invalid") from exc


def _normalize_allowlist_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise EndpointPolicyError("mcp_endpoint_allowlist_invalid")
    value = domain.strip().lstrip("*.").rstrip(".")
    if not value or "://" in value or "/" in value:
        raise EndpointPolicyError("mcp_endpoint_allowlist_invalid")
    try:
        return _normalize_hostname(value)
    except EndpointPolicyError as exc:
        raise EndpointPolicyError("mcp_endpoint_allowlist_invalid") from exc


def _origin(scheme: str, hostname: str, port: int) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{host}:{port}"
