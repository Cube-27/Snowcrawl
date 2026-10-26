"""CONNECT tunnels through a proxy pool (RFC 9110 §9.3.6) for the browser's guard
(`socks_guard`); the HTTP and JSON rungs have curl open theirs (`curl_client`), so every rung reaches a site through a
pool by a tunnel the proxy relays unread.

The tunnel names the address the SSRF guard checked, never a host name: a proxy would resolve a name itself, to an
address the guard never saw.
"""

import asyncio

from snowcrawl.acquire.ssrf import bracketed
from snowcrawl.core.proxy import ProxyEndpoint

__all__ = ["TunnelRefused", "open_tunnel"]

# The longest CONNECT response head read: a proxy that sends more is refused rather than buffered without bound.
_HEAD_MAX_BYTES = 16_384


class TunnelRefused(Exception):
    """The proxy answered a CONNECT with no 2xx, or with no readable response. The message names no host or
    credential."""

    def __init__(self) -> None:
        super().__init__("proxy_refused")


async def open_tunnel(proxy: ProxyEndpoint, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A CONNECT tunnel to `host`:`port`, a checked address, through `proxy`; any 2xx answer opens it. Raises
    `OSError` when the proxy can't be reached and `TunnelRefused` when it refuses the tunnel."""
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port, limit=_HEAD_MAX_BYTES)
    # RFC 9112 §3.2.3: CONNECT names the authority.
    authority_form = f"{bracketed(host)}:{port}"
    head = [f"CONNECT {authority_form} HTTP/1.1", f"Host: {authority_form}"]
    if proxy.authorization is not None:
        head.append(f"Proxy-Authorization: {proxy.authorization}")
    try:
        # An address literal is ASCII.
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        # A 2xx CONNECT response ends with its header section: what follows belongs to the tunnel (RFC 9110 §9.3.6).
        response = await reader.readuntil(b"\r\n\r\n")
    except (OSError, UnicodeEncodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
        writer.close()
        raise TunnelRefused from e
    except BaseException:
        writer.close()  # cancelled (a timeout) while the proxy has not answered: the socket is not left open
        raise
    # RFC 9112 §4: status-line = HTTP-version SP status-code SP [ reason-phrase ].
    fields = response.split(b" ", 2)
    if len(fields) < 2 or not fields[1].startswith(b"2"):
        writer.close()
        raise TunnelRefused
    return reader, writer
