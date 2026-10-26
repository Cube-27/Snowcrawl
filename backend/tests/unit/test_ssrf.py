"""SSRF guard. A stub resolver stands in for DNS; no network."""

import asyncio
import socket
from collections.abc import Sequence
from ipaddress import IPv4Address, ip_address

import pytest

from snowcrawl.acquire.ssrf import IPAddress, RejectReason, SafeConnector, SsrfRejected, Target, is_public

_PUBLIC_V4 = "93.184.215.14"
_PUBLIC_V6 = "2606:2800:21f:cb07::1"

# One address per range the guard must reject, with the standard that reserves it.
_NON_PUBLIC = [
    "10.0.0.1",  # RFC 1918
    "172.16.0.1",  # RFC 1918
    "172.31.255.255",  # RFC 1918
    "192.168.1.1",  # RFC 1918
    "127.0.0.1",  # loopback, RFC 1122
    "0.0.0.0",  # noqa: S104 -- unspecified / "this network", RFC 1122; a value under test, not a bind address
    "169.254.0.1",  # link-local, RFC 3927
    "169.254.169.254",  # cloud metadata, inside link-local
    "100.64.0.1",  # shared address space, RFC 6598
    "192.0.0.1",  # IETF protocol assignments, RFC 6890
    "192.0.2.1",  # documentation, RFC 5737
    "198.18.0.1",  # benchmarking, RFC 2544
    "240.0.0.1",  # reserved, RFC 1112
    "255.255.255.255",  # limited broadcast, RFC 919
    "224.0.0.1",  # multicast, RFC 5771
    "::1",  # loopback, RFC 4291
    "::",  # unspecified, RFC 4291
    "fe80::1",  # link-local, RFC 4291
    "fc00::1",  # unique-local, RFC 4193
    "ff02::1",  # multicast, RFC 4291
    "::ffff:127.0.0.1",  # IPv4-mapped loopback, RFC 4291
    "::ffff:169.254.169.254",  # IPv4-mapped metadata
    "::127.0.0.1",  # IPv4-compatible (deprecated, RFC 4291 §2.5.5.1), inside the IETF-reserved ::/8
    "::93.184.215.14",  # IPv4-compatible is rejected even when the embedded address is public
    "4000::1",  # outside 2000::/3, the only IPv6 global unicast allocation (IANA IPv6 address space)
    "64:ff9b::7f00:1",  # NAT64 well-known prefix embedding 127.0.0.1, RFC 6052
    "2002:7f00:1::1",  # 6to4 embedding 127.0.0.1, RFC 3056
    "2001:db8::1",  # documentation, RFC 3849
    "fec0::1",  # site-local, deprecated by RFC 3879
]


class StubResolver:
    def __init__(self, table: dict[str, Sequence[str]]) -> None:
        self.table = table
        self.calls: list[str] = []

    async def __call__(self, host: str) -> list[IPAddress]:
        self.calls.append(host)
        if host not in self.table:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        return [ip_address(a) for a in self.table[host]]


def _reason(connector: SafeConnector, url: str) -> RejectReason:
    check = connector.check(url)
    with pytest.raises(SsrfRejected) as e:
        asyncio.run(check)
    return e.value.reason


# `is_public` is checked over every range above; the connector's paths to it, over one address of each family and form.
_NON_PUBLIC_SAMPLE = ["10.0.0.1", "::1", "::ffff:169.254.169.254"]


@pytest.mark.parametrize("address", _NON_PUBLIC)
def test_is_public_rejects_reserved_ranges(address: str) -> None:
    assert is_public(ip_address(address)) is False


@pytest.mark.parametrize("address", _NON_PUBLIC_SAMPLE)
def test_literal_reserved_ip_is_rejected_without_dns(address: str) -> None:
    resolver = StubResolver({})
    host = f"[{address}]" if ":" in address else address
    assert _reason(SafeConnector(resolver), f"http://{host}/") == "private_address"
    assert resolver.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,hi",
        "//www.example.com/",
        "www.example.com",
    ],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    assert _reason(SafeConnector(StubResolver({"www.example.com": [_PUBLIC_V4]})), url) == "scheme"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.example.com:22/",
        "http://www.example.com:0/",
        "http://www.example.com:99999/",
    ],
)
def test_ports_outside_the_web_set_are_rejected(url: str) -> None:
    assert _reason(SafeConnector(StubResolver({"www.example.com": [_PUBLIC_V4]})), url) == "port"


# Hosts the WHATWG URL Standard parses as IPv4 although they are not dotted quads.
_NUMERIC_HOSTS = ["2130706433", "0x7f.1", "127.1", "127.0.0.1.", "017700000001", "www.example.0x7f"]
# Hosts parsers read differently: a percent-encoding, and dots IDNA maps to U+002E (UTS #46).
_AMBIGUOUS_HOSTS = ["a%2eexample.test", "127\u30020\u30020\u30021", "a\uff0eexample.test", "a\uff61example.test"]


@pytest.mark.parametrize(
    "url", ["http:///p", "http://:80/", *(f"http://{host}/" for host in (*_NUMERIC_HOSTS, *_AMBIGUOUS_HOSTS))]
)
def test_malformed_or_non_canonical_hosts_are_rejected(url: str) -> None:
    resolver = StubResolver({})
    assert _reason(SafeConnector(resolver), url) == "host"
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("http://www.example.com/", 80),
        ("https://www.example.com/", 443),
        ("https://www.example.com:8443/", 8443),
        ("HTTPS://WWW.EXAMPLE.COM/", 443),
    ],
)
def test_public_target_is_pinned_to_the_checked_address(url: str, port: int) -> None:
    resolver = StubResolver({"www.example.com": [_PUBLIC_V4, _PUBLIC_V6]})
    target = asyncio.run(SafeConnector(resolver).check(url))
    assert target == Target(
        url=url, host="www.example.com", port=port, ip=IPv4Address(_PUBLIC_V4), others=(ip_address(_PUBLIC_V6),)
    )
    assert resolver.calls == ["www.example.com"]


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("http://169.254.169.254/latest/meta-data/", "private_address"),
        ("file:///etc/passwd", "scheme"),
        ("https://www.example.com:22/", "port"),
        # A scheme-relative location keeps the current scheme and is checked like any other.
        (f"//{_NUMERIC_HOSTS[1]}/", "host"),
    ],
)
def test_every_redirect_hop_is_rechecked(location: str, reason: RejectReason) -> None:
    connector = SafeConnector(StubResolver({"www.example.com": [_PUBLIC_V4]}))
    first = asyncio.run(connector.check("https://www.example.com/start"))
    redirect = connector.redirect(first, location)
    with pytest.raises(SsrfRejected) as e:
        asyncio.run(redirect)
    assert e.value.reason == reason


def test_rejection_message_does_not_carry_the_url() -> None:
    # Logs redact URLs when configured; the exception must not leak them.
    connector = SafeConnector(StubResolver({"secret.example.com": ["10.0.0.1"]}))
    check = connector.check("https://secret.example.com/token=abc")
    with pytest.raises(SsrfRejected) as e:
        asyncio.run(check)
    assert str(e.value) == "ssrf_rejected: private_address"
