"""The browser's SSRF guard: a SOCKS5 proxy on loopback that the
browser context sends every connection through.

A request interceptor can't be the guard: the browser follows redirects itself, and interception sees only the
first request of a redirect chain. Behind a SOCKS5 proxy the browser resolves no names (RFC 1928 §4 lets it send the
host name), so each connection, on every redirect hop and from every frame or worker, names its host here. The
guard checks it with `SafeConnector.check` and connects only to the checked address, which is kept for the guard's
later connections: a name is resolved once per fetch, and a changed DNS answer can't redirect a later connection. The
guard keeps the checks of at most `_KEPT` hosts, forgetting the oldest settled one past it, so a long-lived site
profile does not grow without bound; a forgotten host is checked again from scratch.

Only CONNECT with no authentication is spoken (RFC 1928 §3). Bytes are relayed unread, so TLS stays end to end
between the browser and the site.

With an `upstream` proxy pool, the guard still checks each host, then opens the connection as a CONNECT tunnel
through the pool's proxy (`acquire.tunnel`) to the checked address: the proxy resolves no name, so it connects where
the guard checked. The browser's TLS names the host inside the tunnel.
"""

import asyncio
import contextlib
import struct
from collections.abc import Callable, Iterable
from ipaddress import IPv4Address, IPv6Address
from types import TracebackType
from typing import Self, cast

from snowcrawl.acquire.outcome import Traffic
from snowcrawl.acquire.ssrf import RejectReason, SafeConnector, SsrfRejected, Target, authority, bracketed
from snowcrawl.acquire.tunnel import TunnelRefused, open_tunnel
from snowcrawl.core.proxy import ProxyEndpoint

__all__ = ["SocksGuard"]

# RFC 1928 §3 method, §4 command and address types, §6 reply codes.
_VERSION = 5
_NO_AUTH = 0
_NO_ACCEPTABLE_METHOD = 0xFF
_CONNECT = 1
_IPV4, _DOMAIN, _IPV6 = 1, 3, 4
_SUCCEEDED = 0
_GENERAL_FAILURE = 1
_NOT_ALLOWED = 2
_HOST_UNREACHABLE = 4
_COMMAND_NOT_SUPPORTED = 7
_ADDRESS_TYPE_NOT_SUPPORTED = 8

_CHUNK = 65_536
# The hosts a guard keeps checks and refusals for: far more than one page's hosts (its own, CDNs, third parties).
_KEPT = 512
# A check whose name resolution has not answered by then fails as `dns`, as the system resolver gives up after two
# tries of 5 s each by default (resolv.conf(5) `timeout`, `attempts`), so a stalled lookup holds no connection open.
_CHECK_MAX_S = 10.0


class _Refused(Exception):
    def __init__(self, reply: int) -> None:
        super().__init__(reply)
        self.reply = reply


class SocksGuard:
    """`async with SocksGuard(connector) as guard:` listens on `guard.url` until the block ends, then closes the
    listener and every tunnel. `checked` targets were checked by the caller already and are used as they are.
    `upstream` is the proxy pool every connection goes through, if any; `traffic` counts the bytes relayed both ways.
    `rejections` maps each refused (host, port) to the guard's reason."""

    def __init__(
        self,
        connector: SafeConnector,
        checked: Iterable[Target] = (),
        upstream: ProxyEndpoint | None = None,
        traffic: Traffic | None = None,
    ) -> None:
        self._connector = connector
        self._upstream = upstream
        self._traffic = traffic or Traffic()
        self._targets: dict[tuple[str, int], asyncio.Future[Target]] = {}
        for target in checked:
            done: asyncio.Future[Target] = asyncio.get_running_loop().create_future()
            done.set_result(target)
            self._targets[(target.host.lower(), target.port)] = done
        self._handlers: set[asyncio.Task[None]] = set()
        self._server: asyncio.Server | None = None
        self.rejections: dict[tuple[str, int], RejectReason] = {}
        self.address: tuple[str, int] = ("127.0.0.1", 0)  # the listening address, once the guard is entered

    @property
    def url(self) -> str:
        host, port = self.address
        return f"socks5://{host}:{port}"

    def rejection_for(self, url: str) -> RejectReason | None:
        """Why the guard refused the connection `url` needs, if it did."""
        try:
            host, port = authority(url)
        except SsrfRejected:
            return None  # refused before any connection: the guard never saw it
        return self.rejections.get((host.lower(), port))

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.address = ("127.0.0.1", int(self._server.sockets[0].getsockname()[1]))
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()  # closes the listening socket now
        # A check runs apart from the handler that started it (it is shared by every connection to its host), so it
        # is cancelled too: none outlives the fetch.
        checks = [check for check in self._targets.values() if not check.done()]
        for handler in self._handlers:
            handler.cancel()
        for check in checks:
            check.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await asyncio.gather(*checks, return_exceptions=True)
        # Every handler has closed its tunnels. `Server.wait_closed` is not awaited: on Windows a connection the
        # browser reset is never detached from the server (CPython's proactor `_call_connection_lost` raises from
        # `shutdown` before `_detach`), and the wait would never end.

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # asyncio.start_server runs each connection's callback in a task of its own.
        handler = cast(asyncio.Task[None], asyncio.current_task())
        self._handlers.add(handler)
        upstream: asyncio.StreamWriter | None = None
        try:
            if not await _negotiate(reader, writer):
                return
            try:
                upstream_reader, upstream = await self._connect(reader)
            except _Refused as refused:
                writer.write(_reply(refused.reply))
                await writer.drain()
                return
            writer.write(_reply(_SUCCEEDED))
            await writer.drain()
            await asyncio.gather(
                _relay(reader, upstream, self._traffic), _relay(upstream_reader, writer, self._traffic)
            )
        except (OSError, asyncio.IncompleteReadError):
            pass  # the browser or the site went away; the browser sees its connection close
        finally:
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
            self._handlers.discard(handler)

    async def _connect(self, reader: asyncio.StreamReader) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        _, command, _, address_type = await reader.readexactly(4)
        host = await _read_host(reader, address_type)
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        if command != _CONNECT:
            raise _Refused(_COMMAND_NOT_SUPPORTED)
        target = await self._check(host, port)
        if self._upstream is not None:
            return await _tunnel(self._upstream, target)
        return await _direct(target)

    async def _check(self, host: str, port: int) -> Target:
        key = (host.lower(), port)
        if key not in self._targets:
            # The scheme only selects `check`'s rules for web URLs: its port allowlist is the same for http and https,
            # and the tunnel carries whichever the page uses.
            _forget_oldest(self._targets, lambda kept: kept.done())
            self._targets[key] = asyncio.ensure_future(self._bounded(f"https://{bracketed(host)}:{port}/"))
        check = self._targets[key]
        try:
            return await asyncio.shield(check)
        except SsrfRejected as e:
            _forget_oldest(self.rejections, lambda _: True)
            self.rejections[key] = e.reason
            if e.reason == "dns" and self._targets.get(key) is check:
                # A resolver failure may be transient, and a site's context keeps its guard for later pages: the next
                # connection to the host asks again (only this failed check is dropped, never a retry another
                # connection started). A refusal by policy stands.
                del self._targets[key]
            raise _Refused(_NOT_ALLOWED) from e

    async def _bounded(self, url: str) -> Target:
        try:
            async with asyncio.timeout(_CHECK_MAX_S):
                return await self._connector.check(url)
        except TimeoutError as e:
            raise SsrfRejected("dns") from e


def _forget_oldest[K, V](kept: dict[K, V], settled: Callable[[V], bool]) -> None:
    """Make room for one more entry in `kept`, at most `_KEPT` long: the oldest `settled` one goes."""
    if len(kept) < _KEPT:
        return
    oldest = next((key for key, value in kept.items() if settled(value)), None)
    if oldest is not None:
        del kept[oldest]


async def _direct(target: Target) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to the first of `target`'s checked addresses that answers."""
    unreachable: OSError | None = None
    for ip in target.addresses:
        try:
            return await asyncio.open_connection(str(ip), target.port)
        except OSError as e:
            unreachable = e
    raise _Refused(_HOST_UNREACHABLE) from unreachable


async def _tunnel(proxy: ProxyEndpoint, target: Target) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A CONNECT tunnel to `target`'s checked address through `proxy` (`open_tunnel`), its failures as SOCKS5
    replies."""
    try:
        return await open_tunnel(proxy, str(target.ip), target.port)
    except OSError as e:
        raise _Refused(_HOST_UNREACHABLE) from e
    except TunnelRefused as e:
        raise _Refused(_GENERAL_FAILURE) from e


async def _negotiate(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    version, count = await reader.readexactly(2)
    methods = await reader.readexactly(count)
    accepted = version == _VERSION and _NO_AUTH in methods
    writer.write(bytes([_VERSION, _NO_AUTH if accepted else _NO_ACCEPTABLE_METHOD]))
    await writer.drain()
    return accepted


async def _read_host(reader: asyncio.StreamReader, address_type: int) -> str:
    if address_type == _IPV4:
        return str(IPv4Address(await reader.readexactly(4)))
    if address_type == _IPV6:
        return str(IPv6Address(await reader.readexactly(16)))
    if address_type == _DOMAIN:
        (length,) = await reader.readexactly(1)
        try:
            # A browser sends the IDNA (A-label) form of the host (RFC 5891), which is ASCII.
            return (await reader.readexactly(length)).decode("ascii")
        except UnicodeDecodeError as e:
            raise _Refused(_GENERAL_FAILURE) from e
    raise _Refused(_ADDRESS_TYPE_NOT_SUPPORTED)


def _reply(code: int) -> bytes:
    # RFC 1928 §6: BND.ADDR and BND.PORT are unused by a CONNECT client here, so they are zeros.
    return bytes([_VERSION, code, 0, _IPV4]) + bytes(4) + struct.pack("!H", 0)


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, traffic: Traffic) -> None:
    with contextlib.suppress(OSError):
        while data := await reader.read(_CHUNK):
            traffic.bytes += len(data)
            writer.write(data)
            await writer.drain()
    # Either side ending ends the tunnel: neither the browser nor the site waits on a half-closed connection.
    writer.close()
