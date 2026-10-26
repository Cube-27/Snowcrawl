"""Process settings, read from `SNOWCRAWL_*` environment variables.

The variable names are the contract; later specs add settings here.
An empty value counts as unset, so unset CI secrets (which arrive as "") behave like absent variables.
"""

from pathlib import Path
from typing import Annotated, Literal

from curl_cffi.requests import BrowserTypeLiteral
from pydantic import BeforeValidator, Field, IPvAnyNetwork, PositiveFloat, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from snowcrawl.core.proxy import ExitPlace, ProxyPool

# `fs:///<absolute path>` or `s3://<bucket>[/<prefix>]`.
OBJECT_STORE_URL_PATTERN = r"^(fs:///.+|s3://[^/]+(/.*)?)$"
ObjectStoreUrl = Annotated[str, Field(pattern=OBJECT_STORE_URL_PATTERN)]

# The newest Chrome profile curl_cffi supports at lock time.
IMPERSONATE_DEFAULT: BrowserTypeLiteral = "chrome150"


class _Base(BaseSettings):
    # Validation errors leave out the input: settings hold credentials (database and proxy URLs).
    model_config = SettingsConfigDict(
        env_prefix="SNOWCRAWL_", env_ignore_empty=True, frozen=True, hide_input_in_errors=True
    )


class DatabaseSettings(_Base):
    """The subset that migrations need, so running them does not require the rest."""

    database_url: SecretStr


def _comma_separated(value: object) -> object:
    """`a,b` as `("a", "b")`; duplicates are refused (for providers, the order is the failover order)."""
    if not isinstance(value, str):
        return value
    names = tuple(name.strip() for name in value.split(","))
    if len(set(names)) != len(names):
        raise ValueError("a name is listed twice")
    return names


class AcquireSettings(_Base):
    """The subset a fetch needs, so `snowcrawl fetch` runs without a database or object store."""

    impersonate: BrowserTypeLiteral = IMPERSONATE_DEFAULT
    # `SNOWCRAWL_PROXY_POOLS`, JSON `{pool_name: proxy_url | {"endpoints": [proxy_url, ...], "mode": ..., "country"?,
    # "timezone"?, "locale"?}}` (`core.proxy`).
    proxy_pools: dict[str, ProxyPool] = Field(default_factory=dict)
    # `SNOWCRAWL_EGRESS_COUNTRY`, `_TIMEZONE`, `_LOCALE`: the ISO 3166-1 alpha-2 country, IANA time zone and BCP 47
    # locale this machine's direct fetches go out from, declared by the operator (`direct`); unset: undeclared.
    egress_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    egress_timezone: str | None = None
    egress_locale: str | None = None
    # `SNOWCRAWL_DISABLED_ADAPTERS`, comma-separated: the platform adapters that do not run. Every
    # registered adapter runs unless named here (owner, 2026-10-16).
    disabled_adapters: Annotated[tuple[str, ...], NoDecode, BeforeValidator(_comma_separated)] = ()
    # `SNOWCRAWL_BROWSER_PROFILES`: the directory the browser rung keeps its site profiles in, across jobs and processes
    # (`acquire.browser_profiles`); deleting it resets them. Each profile holds its site's cookies in plaintext, so
    # the directory is the operator's to protect. Unset: each profile lasts only while its browser is open.
    browser_profiles: Path | None = None
    # `SNOWCRAWL_BROWSER_HEADLESS`: the browser rung's mode (`acquire.browser_session`). True: headless, presenting the
    # browser's own identity with its headless token renamed, on its site profile's screen. False: headful, a browser
    # window as the Patchright README's best practice runs it; on Linux with no display, on a virtual one
    # (`acquire.display`, Xvfb).
    browser_headless: bool = True
    # `SNOWCRAWL_IMPERSONATE_LAG`: how many Chrome majors curl_cffi's nearest Chrome profile may trail the browser
    # rung's Chrome before startup warns to upgrade curl_cffi (`acquire.impersonate`). Chrome ships a major about every
    # four weeks (Chromium release cycle), so 4 is about a quarter: an unmeasured starting point.
    impersonate_lag: Annotated[int, Field(ge=0)] = 4

    @property
    def direct(self) -> ExitPlace:
        """The place this machine's direct fetches go out from."""
        return ExitPlace(self.egress_country, self.egress_timezone, self.egress_locale)


class ApiSettings(_Base):
    """What the app factory reads: how to admit requests."""

    # JSON list of the proxies whose `Forwarded`/`X-Forwarded-For` name the client (RFC 7239 §8.1).
    trusted_proxies: tuple[IPvAnyNetwork, ...] = ()
    # The per-key token bucket in front of authentication.
    api_rate_per_s: PositiveFloat = 20
    api_rate_burst: PositiveInt = 40


class SearchSettings(_Base):
    """The web search provider (`POST /v1/search`): a SearXNG instance's base URL, or a URL template with
    `{query}` and `{limit}` for any endpoint that answers JSON or HTML. With neither, search answers 503."""

    search_searxng_url: str | None = None
    search_url_template: str | None = None
    # `SNOWCRAWL_SEARCH_FIELDS`, JSON `{"url": member, "title": member, "snippet": member}`: which members of the
    # template provider's JSON results hold each part, when reading them by their shape gets one wrong.
    search_fields: dict[str, str] = Field(default_factory=dict)
    search_timeout_s: PositiveFloat = 15


class McpSettings(_Base):
    """What the MCP server's stdio process reads besides `Settings`: the API key it acts with."""

    api_key: SecretStr


LLMProviderName = Literal["anthropic", "gemini"]


class LLMSettings(_Base):
    """The LLM providers: which, in failover order, and a model per model class.

    The model ids are the current Haiku- and Flash-Lite-class models for `fast` (role C) and Sonnet- and Flash-class
    models for `strong` (A, D, S); change them here, not in code.
    """

    llm_providers: Annotated[tuple[LLMProviderName, ...], NoDecode, BeforeValidator(_comma_separated)] = (
        "anthropic",
        "gemini",
    )
    llm_fast_model_anthropic: str = "claude-haiku-5-5"
    llm_strong_model_anthropic: str = "claude-sonnet-5-5"
    llm_fast_model_gemini: str = "gemini-3.5-flash-lite"
    llm_strong_model_gemini: str = "gemini-3.8-flash"
    # Role A reads each sample page, its JSON key paths and
    # its reduced HTML cut to this many characters in all; a larger limit is refused, so a page's cost is bounded.
    llm_a_max_chars: int = Field(default=60_000, gt=0, le=60_000)
    # From the secret store; required for each listed provider when a client is built.
    llm_api_key_anthropic: SecretStr | None = None
    llm_api_key_gemini: SecretStr | None = None


class Settings(DatabaseSettings, AcquireSettings, ApiSettings, LLMSettings, SearchSettings):
    object_store: ObjectStoreUrl
    # `SNOWCRAWL_COOKIE_KEY`, from the secret store: the Fernet key (urlsafe base64 of 32 bytes) the stored cookie
    # state of temporary browser profiles is encrypted with (`acquire.cookie_store`). Without it none is stored.
    cookie_key: SecretStr | None = None
    log_redact_urls: bool = False
