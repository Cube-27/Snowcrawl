"""Robots.txt (RFC 9309): fetched once per origin per 24 hours (an unreachable one again after `UNREACHABLE_TTL`),
kept in `site.robots_snapshot`, and
read with Protego: RFC 9309 matching (§2.2.2: the longest match wins, `allow` on a tie; §2.2.3: `*` and `$`), plus the
`Crawl-delay` and `Sitemap:` extensions.

Fetches impersonate a browser and announce no product token of their own, so the only group addressed to them is
`*` (RFC 9309 §2.2.1).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from protego import Protego
from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from snowcrawl.acquire.politeness import BACKOFF_MAX
from snowcrawl.acquire.types import FetchResult
from snowcrawl.db.models import Site, SiteId

ROBOTS_TTL: Final = timedelta(hours=24)  # RFC 9309 §2.4 allows caching for up to 24 hours
# RFC 9309 §2.3.1.4 sets no time to ask an unreachable robots.txt again. It is kept as unreachable for the longest
# back-off a slow-down pauses a host for, so each page of the site is not refused only after a request of its own.
UNREACHABLE_TTL: Final = BACKOFF_MAX
# `Crawl-delay` is an extension RFC 9309 leaves undefined. A delay past the longest back-off is read as that back-off,
# as a `Retry-After` past it is (`politeness.backoff`): the host is asked at that slowest rate, never held for good.
CRAWL_DELAY_MAX: Final = BACKOFF_MAX
PARSE_LIMIT_BYTES: Final = 500 * 1024  # RFC 9309 §2.5: parse at least the first 500 KiB
_USER_AGENT: Final = "*"
_TOO_MANY_REQUESTS: Final = 429  # RFC 6585 §4

Fetch = Callable[[str], Awaitable[FetchResult]]


class RobotsSnapshot(BaseModel):
    """One robots.txt as fetched: the rules in force and when they were fetched. An unavailable robots.txt (RFC 9309
    §2.3.1.3) is kept as an empty body, which allows everything; an unreachable one (§2.3.1.4) as no body, which allows
    nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fetched_at: AwareDatetime
    status_code: int
    body: str | None

    @property
    def ttl(self) -> timedelta:
        return UNREACHABLE_TTL if self.body is None else ROBOTS_TTL


# What `site.robots_snapshot` holds, as JSON: a snapshot per robots.txt URL (`robots_url`). One site (host) may be
# served on several origins, and each has its own robots.txt (RFC 9309 §2.3: scheme, host and port).
_SNAPSHOTS: Final = TypeAdapter(dict[str, RobotsSnapshot])


class Robots:
    def __init__(self, body: str) -> None:
        self._parser = Protego.parse(body)

    def allows(self, url: str) -> bool:
        return self._parser.can_fetch(url, _USER_AGENT)

    @property
    def crawl_delay(self) -> timedelta | None:
        # Protego reads no infinite, NaN or negative delay; a finite one past `CRAWL_DELAY_MAX` is capped before it
        # becomes a timedelta, whose range it may pass.
        delay = self._parser.crawl_delay(_USER_AGENT)
        return None if delay is None else timedelta(seconds=min(delay, CRAWL_DELAY_MAX.total_seconds()))

    @property
    def sitemaps(self) -> tuple[str, ...]:
        return tuple(self._parser.sitemaps)


def robots_url(url: str) -> str:
    """The robots.txt that governs `url`: `/robots.txt` at its scheme, host and port (RFC 9309 §2.3)."""
    return at_origin(url, "/robots.txt")


def at_origin(url: str, path: str) -> str:
    """`path` at `url`'s scheme, host and port, without its user information (RFC 3986 §3.2)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def snapshot_of(result: FetchResult) -> RobotsSnapshot | None:
    """The rules a robots.txt fetch yields, with no body when the server answered that it is unreachable, which allows
    nothing (RFC 9309 §2.3.1.4: a 5xx status; and a 429, which asks for fewer requests, RFC 6585 §4, rather than
    saying the file is unavailable). `None` when no answer arrived (a network error, also unreachable): its cause may
    be this side's (a resolver or a timeout of ours), so it is not kept."""
    manifest = result.bundle.manifest
    status, error = manifest.status_code, result.attempts[-1].error
    if status == 0:
        return None
    body: str | None = None
    if 200 <= status < 300 and error is None:
        # RFC 9309 §2.3: UTF-8. A byte order mark (Unicode §23.8) is not part of the first line: kept, it would make
        # the first group's `user-agent` line unreadable.
        body = result.bundle.body[:PARSE_LIMIT_BYTES].decode("utf-8-sig", errors="replace")
    # RFC 9309 §2.3.1.3: a 4xx status means unavailable, as do more than five redirects.
    elif (400 <= status < 500 and status != _TOO_MANY_REQUESTS) or error == "too_many_redirects":
        body = ""
    return RobotsSnapshot(fetched_at=manifest.fetched_at, status_code=status, body=body)


async def site_robots(
    engine: AsyncEngine, site_id: SiteId, stored: str | None, url: str, fetch: Fetch
) -> Robots | None:
    """The robots rules for `url` on its site, from the snapshot of its origin's robots.txt in the site's `stored`
    snapshots while it is fresh (`RobotsSnapshot.ttl`); otherwise fetched, outside any transaction, and stored beside
    the site's other fresh snapshots in a transaction of its own. `None` when robots.txt is unreachable
    (`snapshot_of`)."""
    now = datetime.now(UTC)
    target = robots_url(url)
    fresh = {
        source: snapshot for source, snapshot in _snapshots(stored).items() if now - snapshot.fetched_at < snapshot.ttl
    }
    if (snapshot := fresh.get(target)) is None:
        snapshot = snapshot_of(await fetch(target))
        if snapshot is None:
            return None
        kept = _SNAPSHOTS.dump_json({**fresh, target: snapshot}).decode()
        async with engine.begin() as conn:
            await conn.execute(update(Site).where(Site.id == site_id).values(robots_snapshot=kept))
    return None if snapshot.body is None else Robots(snapshot.body)


def _snapshots(stored: str | None) -> dict[str, RobotsSnapshot]:
    """The snapshots in `stored`. A value of another shape (a single snapshot, stored before origins were kept apart)
    holds none: its origin is unknown, so it is fetched again."""
    if stored is None:
        return {}
    try:
        return _SNAPSHOTS.validate_json(stored)
    except ValidationError:
        return {}
