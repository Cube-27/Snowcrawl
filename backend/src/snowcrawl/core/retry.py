"""What the retry rules of different layers share: the first backoff and the statuses that say "try again"."""

import secrets
from datetime import timedelta
from typing import Final

# The first backoff of a host that asked for fewer requests, and the queue's before it retries a task
# that failed transiently; each retry doubles it.
FIRST_BACKOFF: Final = timedelta(seconds=30)
# The OS's source of randomness: a jitter needs no reproducible sequence, and none seeded by the process.
_RANDOM: Final = secrets.SystemRandom()


def backoff_delay(tries: int) -> timedelta:
    """The backoff after try `tries` (1 for the first) failed, with full jitter: uniformly random between none and
    `FIRST_BACKOFF` doubled per try (AWS Architecture Blog, "Exponential Backoff And Jitter", 2015), so the clients
    that failed together do not retry together. The queue retries a task after it, and the polite fetch pauses a host
    by it (`acquire.politeness.backoff`)."""
    ceiling: timedelta = FIRST_BACKOFF * 2 ** (tries - 1)
    return ceiling * _RANDOM.random()


def is_transient(status: int) -> bool:
    """Whether an HTTP status says the server did not answer this time: 408 Request Timeout (RFC 9110 §15.5.9),
    429 Too Many Requests (RFC 6585 §4) or a 5xx server error (RFC 9110 §15.6). Other errors are the request's own
    fault (a bad key, an unknown model, a refused schema)."""
    return status in (408, 429) or 500 <= status <= 599
