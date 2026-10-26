"""Browser fetch: a JS-only page is rendered, the JSON it loads is captured within the caps, every
connection passes the SSRF guard, and the page and its workers present Chrome's own identity.

A local server on 127.0.0.1 serves synthetic pages under `.test` hosts; no live network. The connector admits only
those hosts (and the server's own address) at the server's port; every other name goes through the real guard with
a stub resolver. One browser serves the whole module, as one serves a worker process.

The browser runs with Playwright's own forced proxying of loopback turned off, so the loopback and link-local tests
prove that `fetch_browser` itself keeps those destinations behind the guard.
"""

import asyncio
import gzip
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, ip_address
from urllib.parse import urlsplit

import pytest
from lxml import html as lxml_html
from patchright.async_api import Error as PlaywrightError
from pydantic import TypeAdapter

from snowcrawl.acquire.browser import fetch_browser
from snowcrawl.acquire.browser_capture import CAPS, Caps
from snowcrawl.acquire.browser_profiles import profile_name
from snowcrawl.acquire.browser_session import BrowserSession, context_key, open_browser
from snowcrawl.acquire.desktop_screen import desktop_screen
from snowcrawl.acquire.session import CookieExport, SiteSessions
from snowcrawl.acquire.ssrf import IPAddress, SafeConnector, SsrfRejected, Target
from snowcrawl.acquire.types import FetchPolicy, FetchResult
from snowcrawl.config_ir.page_actions import Action, to_steps
from snowcrawl.core.engine_version import ENGINE_VERSION
from snowcrawl.core.proxy import ProxyPool
from snowcrawl.storage.capture_bundle import decode, encode

pytestmark = pytest.mark.browser

_HTML = "text/html; charset=utf-8"
_JSON = "application/json"
_APP = b"""<html><body><nav><a href="/">Home</a></nav><div id="root"></div><script>
fetch('/api/items').then(r => r.json()).then(items => {
  let html = '';
  for (const item of items) html += '<p>' + item['name'] + '</p>';
  document.getElementById('root').innerHTML = html;
  return Promise.all([fetch('/api/note'), fetch('/style.css'), fetch('/api/flight')]);
});
</script></body></html>"""
# Enough rendered text that the page is not a shell (200 visible characters).
_ITEMS = json.dumps([{"name": f"Catalogue item {i} with a short description"} for i in range(12)]).encode()
# A React Server Components Flight payload (React's row grammar), kept beside the JSON.
_FLIGHT = b'0:{"price":"$$19.99"}\n'
_FLIGHT_TYPE = "text/x-component"
_MANY = b"""<html><body><p id="done"></p><script>
(async () => {
  await fetch('/api/big');
  for (let i = 0; i < 55; i++) await fetch('/api/n/' + i);
  document.getElementById('done').textContent = 'loaded';
})();
</script></body></html>"""
_SEARCH = b"""<html><body><p id="done"></p><script>
fetch('/api/search', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{"q": "lamp"}'})
  .then(r => r.json()).then(() => { document.getElementById('done').textContent = 'searched'; });
</script></body></html>"""
# Data loaded as JSON under other labels: behind a guard prefix, as text/plain, and as an XMLHttpRequest; beside a
# text/plain body that is no JSON, an empty JSON answer and a stylesheet fetched with fetch() (its body is not data).
_SNIFFED = b"""<html><body><p id="done"></p><script>
(async () => {
  await fetch('/api/guarded');
  await fetch('/api/plain-json');
  await fetch('/api/note');
  await fetch('/api/empty');
  await fetch('/style.css');
  await new Promise(done => {
    const x = new XMLHttpRequest(); x.onload = done; x['open']('GET', '/api/linked'); x.send();
  });
  document.getElementById('done').textContent = 'loaded';
})();
</script></body></html>"""
_GUARDED = b")]}'\n" + json.dumps({"items": [{"name": "Guarded item"}]}).encode()
# An engineer capture's page: two calls to its own site in turn (one guarded), one to another site, an image fetched
# as data, and a form a step submits.
_ENGINEER = b"""<html><body><main><p>A page whose calls an engineer reads, with enough text to be content.</p>
<form method="post" action="/result"><input type="hidden" name="q" value="shoes"><button id="go">Go</button></form>
<script>
(async () => {
  await (await fetch('/api/items')).text();
  await (await fetch('/api/guarded')).text();
  await (await fetch('/pixel.png')).blob();
  await (await fetch('http://api.other.test:' + location.port + '/api/n/3')).text();
})();
</script></main></body></html>"""
_RESULT = b"<html><body><main><p>The results the form asked for, with enough text to be read.</p></main></body></html>"
# Six bodies of 1.9 MB each: past 10 MB in all at the sixth.
_HEAVY = b"""<html><body><p id="done"></p><script>
(async () => {
  for (let i = 0; i < 6; i++) await fetch('/api/heavy/' + i);
  document.getElementById('done').textContent = 'loaded';
})();
</script></body></html>"""
_HEAVY_BODY = b'{"pad": "' + b"x" * 1_900_000 + b'"}'
# A data call whose body never ends (its server sends the headers and part of the body, then holds the rest past the
# fetch), beside one that answers in full.
_UNENDING_CALL = (
    b'<html><body><nav><a href="/">Home</a></nav><p>'
    + b"A page whose content is static while one of its data calls never ends. " * 4
    + b"</p><script>fetch('/api/unending'); fetch('/api/items');</script></body></html>"
)
# A page that starts a dedicated worker (HTML Standard §10.2.6), which makes a request of its own, holds a frame of
# another site (`localhost`: a cross-site frame renders in a process of its own), and writes the client hints it reads,
# then those a service worker (Service Workers §3.1) and a shared worker (HTML Standard §10.2.6) report once running.
# 127.0.0.1 and localhost are potentially trustworthy origins (W3C Secure Contexts §3.2), to which client hints go and
# which may register a service worker.
_WITH_WORKER = """<html><body><p>Worker page</p><iframe src="http://localhost:{port}/framed"></iframe><script>
const write = (id, text) => {{
  const pre = document.createElement('pre'); pre.setAttribute('id', id); pre.textContent = text;
  document.body.append(pre);
}};
new Worker('/worker.js');
navigator.userAgentData.getHighEntropyValues({hints}).then(h => write('hints', JSON.stringify(h)));
navigator.serviceWorker.onmessage = e => write('service-worker', e['data']);
navigator.serviceWorker.register('/service-worker.js').then(() => navigator.serviceWorker.ready)
  .then(r => r.active.postMessage({hints}));
const shared = new SharedWorker('/shared-worker.js');
shared.port.onmessage = e => write('shared-worker', e['data']);
shared.port.postMessage({hints});
</script></body></html>"""
# What a worker reports when asked: its user agent and the client hints the page names.
_REPORT = b"""const report = h => navigator.userAgentData.getHighEntropyValues(h)
  .then(v => JSON.stringify([navigator.userAgent, v]));
"""
_SERVICE_WORKER = _REPORT + b"self.onmessage = e => e.waitUntil(report(e['data']).then(r => e.source.postMessage(r)));"
_SHARED_WORKER = (
    _REPORT
    + b"""self.onconnect = c => {
  c.ports[0].onmessage = e => report(e['data']).then(r => c.ports[0].postMessage(r));
};"""
)
# A page that writes down the screen, window and viewport it is laid out in (CSSOM View), at its first rendering
# opportunity (HTML Standard §8.1.7.3 `requestAnimationFrame`). Before it, Chrome, headed or headless, may report an
# `outerWidth` and `outerHeight` of 0: a navigation's new document gets a new widget, which learns its window's
# rectangle only when the browser shows it after the commit, so a parse-time script races that message (Chromium
# `RenderWidgetHostImpl::SendScreenRects`). Measured on Google Chrome 156 with an inline head script, 0 in 26 of 39
# page-driven navigations headless and 22 of 39 headed.
_GEOMETRY = b"""<html><body><p>Geometry page</p><pre id="geometry"></pre><script>
requestAnimationFrame(() => document.getElementById('geometry').textContent = JSON.stringify({
  width: screen.width, height: screen.height, availWidth: screen.availWidth, availHeight: screen.availHeight,
  outerWidth, outerHeight, innerWidth, innerHeight, devicePixelRatio, screenX, screenY,
}));
</script></body></html>"""
_FRAMED = b"<html><body><p>Framed</p><script>fetch('/api/n/8');</script></body></html>"

# A 403 document that stands in for the page and never navigates on within the fetch (a challenge it cannot pass).
# A 403 document that is a site page: enough text and hyperlinks (`acquire/classify.py`).
_REFUSAL_PAGE = (
    b"<html><body><nav>"
    + b"".join(b'<a href="/c/%d">Section %d</a>' % (i, i) for i in range(6))
    + b"</nav><p>"
    + b"This page is not available to you. " * 8
    + b"</p></body></html>"
)


# A page taller than the browser's viewport on any of its screens, with enough text not to be a shell.
_TALL = (
    b'<html><body><nav><a href="/">Home</a></nav><p>'
    + b"A paragraph of the tall page that is long enough to be read as content. " * 6
    + b"</p>"
    b'<div style="height: 3000px; background: #ccd"></div><p>The end of the tall page.</p></body></html>'
)


@dataclass(frozen=True)
class Reply:
    status: int = 200
    content_type: str = _HTML
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    delay_s: float = 0.0  # before the status line
    hold_s: float = 0.0  # after the first half of the body, before the rest


@dataclass
class Server:
    port: int
    routes: dict[str, Reply] = field(default_factory=dict)
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)  # (host, path, headers)

    def url(self, path: str, host: str = "shop.example.test") -> str:
        return f"http://{host}:{self.port}{path}"


def _reply_for(server: Server, path: str) -> Reply:
    if path in server.routes:
        return server.routes[path]
    if path.startswith("/api/heavy/"):
        return Reply(content_type=_JSON, body=_HEAVY_BODY)
    if path.startswith("/api/n/"):
        # Another site's page may call it (Fetch Standard §3.2: CORS).
        body = json.dumps({"n": int(path.rsplit("/", 1)[1])}).encode()
        return Reply(content_type=_JSON, body=body, headers=(("Access-Control-Allow-Origin", "*"),))
    return Reply(404, body=b"<html><body>not here</body></html>")


@pytest.fixture(scope="module")
def server() -> Iterator[Server]:
    state: list[Server] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            srv = state[0]
            srv.requests.append((self.headers.get("Host", ""), self.path, dict(self.headers.items())))
            reply = _reply_for(srv, self.path)
            time.sleep(reply.delay_s)
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.content_type)
            for name, value in reply.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            half = len(reply.body) // 2
            self.wfile.write(reply.body[:half])
            if reply.hold_s:
                self.wfile.flush()
                time.sleep(reply.hold_s)
            self.wfile.write(reply.body[half:])

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    srv = Server(port=httpd.server_address[1])
    srv.routes.update(
        {
            "/app": Reply(body=_APP, headers=(("Set-Cookie", "session=s1"),)),
            "/api/items": Reply(content_type=_JSON, body=_ITEMS),
            "/api/note": Reply(content_type="text/plain", body=b"plain text"),
            "/api/flight": Reply(content_type=_FLIGHT_TYPE, body=_FLIGHT),
            "/style.css": Reply(content_type="text/css", body=b"p { color: black }"),
            "/api/big": Reply(content_type=_JSON, body=b"[" + b"0," * 1_100_000 + b"0]"),
            "/tall": Reply(body=_TALL),
            "/geometry": Reply(body=_GEOMETRY),
            "/many": Reply(body=_MANY),
            "/search": Reply(body=_SEARCH),
            "/api/search": Reply(content_type=_JSON, body=b'{"hits": []}'),
            "/sniffed": Reply(body=_SNIFFED),
            "/api/guarded": Reply(content_type="text/plain; charset=utf-8", body=_GUARDED),
            "/engineer": Reply(body=_ENGINEER),
            "/result": Reply(body=_RESULT),
            "/pixel.png": Reply(content_type="image/png", body=b"\x89PNG\r\n\x1a\n"),
            "/api/plain-json": Reply(content_type="text/plain", body=b'[{"id": 1}, {"id": 2}]'),
            "/api/empty": Reply(204, content_type=_JSON),
            "/api/linked": Reply(
                content_type="application/vnd.example+json",
                body=b'{"page": 1}',
                headers=(("Link", '</api/linked?page=2>; rel="next"'), ("Set-Cookie", "session=s2")),
            ),
            "/heavy": Reply(body=_HEAVY),
            "/unending-call": Reply(body=_UNENDING_CALL),
            "/api/unending": Reply(content_type=_JSON, body=b'{"never": "complete"}', hold_s=30),
            "/framed": Reply(body=_FRAMED),
            "/worker.js": Reply(content_type="text/javascript", body=b"fetch('/api/n/7');"),
            "/service-worker.js": Reply(content_type="text/javascript", body=_SERVICE_WORKER),
            "/shared-worker.js": Reply(content_type="text/javascript", body=_SHARED_WORKER),
            "/refusal": Reply(403, body=_REFUSAL_PAGE),
            "/stall": Reply(body=b"<html></html>", delay_s=8),
            "/hop": Reply(302, headers=(("Location", "/app"),)),
            "/hop-private": Reply(302, headers=(("Location", "http://internal.example.com/"),)),
        }
    )
    state.append(srv)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    httpd.shutdown()
    httpd.server_close()


class LocalConnector(SafeConnector):
    """Admits `.test` hosts, 127.0.0.1 and localhost at the server's port, pinned to 127.0.0.1; everything else goes
    through the real guard, where `internal.example.com` resolves to a private address."""

    def __init__(self, port: int) -> None:
        async def resolve(host: str) -> list[IPAddress]:
            if host != "internal.example.com":
                raise OSError("no such host")
            return [ip_address("10.0.0.1")]

        super().__init__(resolve)
        self._port = port

        self.refused: list[tuple[str, int | None]] = []  # (host, port) of every URL the guard refused

    async def check(self, url: str) -> Target:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (host.endswith(".test") or host in ("127.0.0.1", "localhost")) and parts.port == self._port:
            return Target(url=url, host=host, port=self._port, ip=IPv4Address("127.0.0.1"))
        try:
            return await super().check(url)
        except SsrfRejected:
            self.refused.append((host, parts.port))
            raise


@dataclass
class Browser:
    runner: asyncio.Runner
    session: BrowserSession
    port: int

    connector: LocalConnector | None = None  # the last fetch's

    def fetch(
        self,
        url: str,
        policy: FetchPolicy | None = None,
        *,
        export: CookieExport | None = None,
        pools: dict[str, ProxyPool] | None = None,
    ) -> FetchResult:
        self.connector = LocalConnector(self.port)
        policy = policy or FetchPolicy(browser_timeout_s=10)
        return self.runner.run(
            fetch_browser(
                url,
                policy,
                session=self.session,
                engine_version=ENGINE_VERSION,
                connector=self.connector,
                pools=pools,
                export=export,
                scope=SiteSessions().scope_for(policy),
            )
        )


@pytest.fixture(scope="module")
def browser(server: Server) -> Iterator[Browser]:
    yield from _browser(server, headless=True)


@pytest.fixture(scope="module", params=[True, False], ids=["headless", "headful"])
def each_mode(request: pytest.FixtureRequest, server: Server) -> Iterator[Browser]:
    """The module's headless browser, then a headful one (`SNOWCRAWL_BROWSER_HEADLESS=false`), on a virtual display
    where the machine has none."""
    if request.param:
        yield request.getfixturevalue("browser")
    else:
        yield from _browser(server, headless=False)


def _browser(server: Server, *, headless: bool) -> Iterator[Browser]:
    with pytest.MonkeyPatch.context() as env, asyncio.Runner() as runner:
        # Read by the Playwright driver when it starts (crBrowser `shouldProxyLoopback`).
        env.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
        opened = open_browser(headless=headless)
        try:
            session = runner.run(opened.__aenter__())
        except PlaywrightError as e:
            if os.environ.get("CI") or "is not found at" not in e.message:
                raise
            pytest.skip("Google Chrome is not installed: run `uv run patchright install chrome`")
        try:
            yield Browser(runner, session, server.port)
            # Each fetch hands its profile back, so none is left rendering between tests.
            assert session.profiles.busy == 0
        finally:
            runner.run(opened.__aexit__(None, None, None))
        assert not session.profiles.idle


def test_js_only_page_is_rendered_and_its_json_and_flight_captured(browser: Browser, server: Server) -> None:
    result = browser.fetch(server.url("/app"))

    assert (result.method, result.block_class, result.bundle.manifest.status_code) == ("browser", "ok", 200)
    assert b"<p>Catalogue item 0 with a short description</p><p>Catalogue item 1 with" in result.bundle.body
    assert result.bundle.raw_body == _APP
    entries = result.bundle.manifest.network_bodies
    assert [(e.url, e.status, e.content_type, e.file, e.method, e.request_body) for e in entries] == [
        (server.url("/api/items"), 200, _JSON, "network/0001.json", "GET", None),
        (server.url("/api/flight"), 200, _FLIGHT_TYPE, "network/0002.json", "GET", None),
    ]
    assert result.bundle.network == (_ITEMS, _FLIGHT)
    assert decode(encode(result.bundle)) == result.bundle


def test_an_engineer_capture_keeps_every_call_own_site_first_and_the_document_a_step_submitted(
    browser: Browser, server: Server
) -> None:
    steps = to_steps(TypeAdapter(list[Action]).validate_python([{"type": "click", "selector": "#go"}]), None)
    policy = FetchPolicy(method_hint="browser", browser_timeout_s=10, steps=steps, calls="all")

    result = browser.fetch(server.url("/engineer"), policy)

    entries = result.bundle.manifest.network_bodies
    kept = [(e.url, e.resource_type, e.action, e.same_site, e.method, e.request_body) for e in entries]
    assert kept == [
        (server.url("/api/items"), "fetch", 0, True, "GET", None),
        (server.url("/api/guarded"), "fetch", 0, True, "GET", None),
        (server.url("/result"), "document", 1, True, "POST", "q=shoes"),
        (server.url("/api/n/3", host="api.other.test"), "fetch", 0, False, "GET", None),
    ]
    assert [e.guard_prefix for e in entries] == [None, ")]}'\n", None, None]
    assert result.bundle.network[2] == _RESULT
    assert result.attempts[-1].steps[0].outcome == "done"
    assert decode(encode(result.bundle)) == result.bundle


def test_an_engineer_capture_keeps_its_own_site_s_calls_first_within_its_cap(
    browser: Browser, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(CAPS, "all", Caps(bodies=1, page_bytes=CAPS["all"].page_bytes))
    policy = FetchPolicy(method_hint="browser", browser_timeout_s=10, calls="all")

    result = browser.fetch(server.url("/engineer"), policy)

    assert [e.url for e in result.bundle.manifest.network_bodies] == [server.url("/api/items")]


def test_a_data_call_whose_body_never_ends_is_left_out_and_the_page_kept(browser: Browser, server: Server) -> None:
    result = browser.fetch(server.url("/unending-call"))

    assert (result.block_class, result.attempts[-1].error) == ("ok", None)
    assert [e.url for e in result.bundle.manifest.network_bodies] == [server.url("/api/items")]


def test_the_probe_reads_what_every_request_of_a_page_its_frame_and_workers_presents(
    each_mode: Browser, server: Server
) -> None:
    """Owner 2026-10-23: in either mode, the identity read at startup (`browser_identity`), which the HTTP and JSON
    rungs send, names no headless mode, and is what the page, a cross-site frame and a dedicated, service and shared
    worker send and report."""
    browser = each_mode
    identity = browser.session.identity
    hints = ["architecture", "bitness", "fullVersionList", "model", "platformVersion", "wow64"]
    server.routes["/with-worker"] = Reply(body=_WITH_WORKER.format(port=server.port, hints=json.dumps(hints)).encode())
    seen = len(server.requests)

    result = browser.fetch(server.url("/with-worker", host="127.0.0.1"))

    assert "headless" not in json.dumps([identity.headers, identity.metadata]).lower()
    sent = {
        path: {name.lower(): value for name, value in headers.items()} for _, path, headers in server.requests[seen:]
    }
    scripts = {"/worker.js", "/service-worker.js", "/shared-worker.js"}
    assert set(sent) >= {"/with-worker", "/framed", "/api/n/7", "/api/n/8", *scripts}
    assert {headers["user-agent"] for headers in sent.values()} == {identity.agent}
    named = {name.lower(): value for name, value in identity.headers}
    for document in ("/with-worker", "/framed"):
        assert {name: sent[document].get(name) for name in named} == named
    page = lxml_html.fromstring(result.bundle.body)
    reported = json.loads(page.get_element_by_id("hints").text_content())
    assert {name: reported[name] for name in hints} == {name: identity.metadata[name] for name in hints}
    for worker in ("service-worker", "shared-worker"):
        assert json.loads(page.get_element_by_id(worker).text_content()) == [identity.agent, reported]


def test_a_page_is_laid_out_in_a_maximised_window_on_the_screen_of_its_profile(
    browser: Browser, server: Server
) -> None:
    """Owner 2026-10-23: the screen is the site profile's (`acquire.desktop_screen`), and what the page reads of it is
    one desktop's: a work area of the screen less its platform's bar, the window filling it, the viewport inside its
    toolbars."""
    url = server.url("/geometry")
    key = context_key(url, SiteSessions().scope_for(FetchPolicy()), None)
    assert key is not None
    screen = desktop_screen(profile_name(*key), browser.session.identity.platform)

    result = browser.fetch(url)

    read = json.loads(lxml_html.fromstring(result.bundle.body).get_element_by_id("geometry").text_content())
    assert (read["width"], read["height"], read["devicePixelRatio"]) == (screen.width, screen.height, screen.scale)
    assert (read["availWidth"], read["screenX"], read["screenY"]) == (screen.width, 0, screen.top)
    work_area = screen.height - screen.top - screen.bottom
    assert work_area == read["availHeight"] == read["outerHeight"] > read["innerHeight"]
    assert read["outerWidth"] == read["innerWidth"] == screen.width


@pytest.fixture(scope="module")
def other_loopback_server() -> Iterator[Server]:
    """A JSON service on another loopback port, which the connector does not admit."""
    srv: Server | None = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert srv is not None
            srv.requests.append((self.headers.get("Host", ""), self.path, dict(self.headers.items())))
            self.send_response(200)
            self.send_header("Content-Type", _JSON)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"secret": 1}')

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    srv = Server(port=httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    httpd.shutdown()
    httpd.server_close()


def _subresources_page(target: str) -> bytes:
    # The page reaches for `target` three ways: fetch, XHR and an iframe.
    return f"""<html><body><p>Page with subresources</p><iframe src="{target}frame"></iframe><script>
fetch('{target}fetch').catch(() => {{}});
const xhr = new XMLHttpRequest(); xhr['open']('GET', '{target}xhr'); xhr['send']();
</script></body></html>""".encode()


@pytest.mark.parametrize(
    ("host", "reason"),
    [
        # RFC 3927 link-local, the cloud metadata address; RFC 1122 §3.2.1.3 loopback. Chromium's implicit proxy
        # bypass rules (`ProxyBypassRules`, net/proxy_resolution) cover both.
        pytest.param("169.254.169.254", "link-local", id="link-local"),
        pytest.param("127.0.0.1", "loopback", id="loopback"),
    ],
)
def test_a_subresource_to_a_loopback_or_link_local_address_is_refused_by_the_guard(
    browser: Browser, server: Server, other_loopback_server: Server, host: str, reason: str
) -> None:
    port = other_loopback_server.port if host == "127.0.0.1" else 80
    target = f"http://{host}:{port}/"
    path = f"/subresources-{reason}"
    server.routes[path] = Reply(body=_subresources_page(target))

    result = browser.fetch(server.url(path), FetchPolicy(browser_timeout_s=10))

    assert result.attempts[0].error is None  # the page itself loads; only its subresources are refused
    assert b"Page with subresources" in result.bundle.body
    assert browser.connector is not None
    # The guard names the port explicitly in the URL it checks.
    assert (host, port) in browser.connector.refused
    assert other_loopback_server.requests == []
    assert result.bundle.manifest.network_bodies == ()


# --- cookies stay inside one fetch -----------------------------------------------------------------------


# --- challenges and proxy health -------------------------------------------------------------------


# --- untrusted bodies are bounded --------------------------------------------------------------------------


def test_inv30_a_body_over_the_cap_is_an_error_without_a_body(browser: Browser, server: Server) -> None:
    server.routes["/inv30/large"] = Reply(body=b"<html><body><p>" + b"x" * 50_000 + b"</p></body></html>")

    result = browser.fetch(server.url("/inv30/large"), FetchPolicy(max_body_bytes=10_000, browser_timeout_s=10))

    (attempt,) = result.attempts
    assert (attempt.status, attempt.block_class, attempt.error) == (200, "error", "body_cap")
    assert (result.bundle.body, result.bundle.raw_body, result.bundle.network) == (b"", None, ())


def test_inv30_a_compression_bomb_stops_at_the_decompressed_cap_without_a_body(
    browser: Browser, server: Server
) -> None:
    bomb = gzip.compress(b"<html><body><p>" + b"x" * 1_000_000 + b"</p></body></html>")  # about 1 kB on the wire
    server.routes["/inv30/bomb"] = Reply(body=bomb, headers=(("Content-Encoding", "gzip"),))
    policy = FetchPolicy(max_body_bytes=1_000_000, max_decompressed_bytes=100_000, browser_timeout_s=10)

    result = browser.fetch(server.url("/inv30/bomb"), policy)

    (attempt,) = result.attempts
    assert (attempt.status, attempt.block_class, attempt.error) == (200, "error", "body_cap")
    assert (result.bundle.body, result.bundle.raw_body, result.bundle.network) == (b"", None, ())


# --- settling follows a navigation the page starts itself ------------------------------------------------------------


def test_a_page_that_navigates_itself_during_the_settle_is_captured_as_its_final_document(
    browser: Browser, server: Server
) -> None:
    shell = (
        b"<html><body><p>Interim shell</p>"
        b"<script>setTimeout(() => location.replace('/self-final'), 200);</script></body></html>"
    )
    server.routes["/self-nav"] = Reply(404, body=shell)
    server.routes["/self-final"] = Reply(body=b"<html><body><p>Final document</p></body></html>")

    result = browser.fetch(server.url("/self-nav"))

    assert b"Final document" in result.bundle.body
    assert b"Interim shell" not in result.bundle.body


def test_a_2xx_shell_that_navigates_itself_after_the_ready_wait_is_waited_for(browser: Browser, server: Server) -> None:
    # A check served with 200: no links, little text, a script that moves on well after the document is ready.
    check = b"<html><body><p>One moment</p><script>setTimeout(() => location.replace('/checked'), 2000);</script>"
    server.routes["/check"] = Reply(body=check + b"</body></html>")
    links = "".join(f'<a href="/p/{i}">Page {i}</a>' for i in range(6))
    page = f"<html><body><p>{'Checked page text. ' * 20}</p>{links}</body></html>"
    server.routes["/checked"] = Reply(body=page.encode())

    result = browser.fetch(server.url("/check"))

    assert (result.block_class, result.bundle.manifest.final_url) == ("ok", server.url("/checked"))
