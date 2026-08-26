from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

from .contracts import EndpointMode, ProviderEndpoint


class EndpointPolicyError(ValueError):
    pass


Resolver = Callable[[str, int], Iterable[str]]


def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    values = []
    for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = str(item[4][0])
        if address not in values:
            values.append(address)
    return tuple(values)


@dataclass(frozen=True)
class ValidatedEndpoint:
    base_url: str
    host: str
    port: int
    resolved_addresses: tuple[str, ...]
    mode: EndpointMode


class EndpointPolicy:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        allow_custom_remote: bool = True,
        allowed_remote_ports: frozenset[int] | None = None,
    ) -> None:
        self.resolver = resolver or _system_resolver
        self.allow_custom_remote = allow_custom_remote
        self.allowed_remote_ports = allowed_remote_ports or frozenset({443, 8443})

    def validate(
        self,
        endpoint: ProviderEndpoint,
        *,
        resolve_dns: bool = True,
    ) -> ValidatedEndpoint:
        parsed = urlsplit(endpoint.base_url)
        if parsed.username or parsed.password:
            raise EndpointPolicyError("provider endpoint cannot contain userinfo")
        if parsed.query or parsed.fragment:
            raise EndpointPolicyError("provider endpoint cannot contain query or fragment")
        if not parsed.hostname:
            raise EndpointPolicyError("provider endpoint requires a hostname")
        host = parsed.hostname.rstrip(".").lower()
        if not host or "*" in host:
            raise EndpointPolicyError("provider endpoint hostname is invalid")
        if endpoint.mode == EndpointMode.LOCAL:
            if parsed.scheme not in {"http", "https"}:
                raise EndpointPolicyError("local provider endpoint must use HTTP or HTTPS")
            if not self._is_loopback_host(host):
                raise EndpointPolicyError("local provider endpoint must use loopback")
        else:
            if endpoint.custom and not self.allow_custom_remote:
                raise EndpointPolicyError("custom remote provider endpoints are disabled")
            if parsed.scheme != "https":
                raise EndpointPolicyError("remote provider endpoint must use HTTPS")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise EndpointPolicyError("provider endpoint port is invalid") from exc
        if endpoint.mode == EndpointMode.REMOTE and port not in self.allowed_remote_ports:
            raise EndpointPolicyError("remote provider endpoint port is not allowed")

        addresses: tuple[str, ...] = ()
        literal = self._as_ip(host)
        if literal is not None:
            self._validate_address(literal, endpoint.mode)
            addresses = (str(literal),)
        elif resolve_dns:
            try:
                addresses = tuple(self.resolver(host, port))
            except OSError as exc:
                raise EndpointPolicyError("provider endpoint DNS resolution failed") from exc
            if not addresses:
                raise EndpointPolicyError("provider endpoint DNS returned no addresses")
            for address in addresses:
                try:
                    self._validate_address(ipaddress.ip_address(address), endpoint.mode)
                except ValueError as exc:
                    raise EndpointPolicyError("provider endpoint DNS returned an invalid address") from exc
        return ValidatedEndpoint(
            base_url=endpoint.base_url.rstrip("/"),
            host=host,
            port=port,
            resolved_addresses=addresses,
            mode=endpoint.mode,
        )

    @staticmethod
    def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            return None

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        if host == "localhost":
            return True
        address = EndpointPolicy._as_ip(host)
        return bool(address and address.is_loopback)

    @staticmethod
    def _validate_address(
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        mode: EndpointMode,
    ) -> None:
        if mode == EndpointMode.LOCAL:
            if not address.is_loopback:
                raise EndpointPolicyError("local provider DNS must resolve only to loopback")
            return
        if not address.is_global:
            raise EndpointPolicyError("remote provider endpoint resolved to a non-public address")
