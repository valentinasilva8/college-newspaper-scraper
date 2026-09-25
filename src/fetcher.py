"""Shared HTTP layer for all site extractors.

Responsibilities:
- Polite, configurable per-request delay (default 1-3s, randomized).
- Bounded concurrency via a Semaphore. For this pilot the bound is 1
  (sequential, one request at a time per domain) -- see note below.
- Exponential backoff + retries on transient failures.
- Reads and respects robots.txt before every fetch, with explicit
  behavior for the 404 and fetch-error cases.
- Sends an honest research User-Agent on every request (and uses the
  same string when evaluating robots.txt rules).
- Backs off when a site starts refusing us (403/429): after
  ``block_threshold`` consecutive refusals it sleeps through escalating
  cooldowns, probing once after each, and raises ``SiteBlockedError`` when
  every cooldown is used up. A refusing site is never pushed through.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Honest, consistent research User-Agent. Used for BOTH the robots.txt
# evaluation and every outbound request -- we do not impersonate a browser.
USER_AGENT = (
    "CollegeNewspaperResearchBot/1.0 "
    "(academic research; contact: valentinatsilva@proton.me)"
)

# Responses that mean "the site is refusing us", as opposed to one bad URL.
BLOCK_STATUSES = frozenset({403, 429})
# 15 min, 30 min, 1 h, 2 h: ~3.75 h of patience before giving up on a site.
DEFAULT_BLOCK_COOLDOWNS = (900.0, 1800.0, 3600.0, 7200.0)


class SiteBlockedError(RuntimeError):
    """The site kept refusing requests after every cooldown.

    Deliberately not a ``requests.RequestException``: extractors catch those
    per URL and move on, which is exactly what must not happen here.
    """


def _retry_after_seconds(resp: requests.Response) -> float:
    value = (resp.headers or {}).get("Retry-After", "")
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


class Fetcher:
    """Rate-limited, robots-aware HTTP client shared by every extractor."""

    def __init__(
        self,
        delay_min: float = 1.0,
        delay_max: float = 3.0,
        max_concurrency: int = 1,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        timeout: float = 15.0,
        user_agent: str = USER_AGENT,
        block_threshold: int = 5,
        block_cooldowns: tuple[float, ...] = DEFAULT_BLOCK_COOLDOWNS,
    ) -> None:
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.timeout = timeout
        self.user_agent = user_agent
        self.block_threshold = max(int(block_threshold), 1)
        self.block_cooldowns = tuple(float(s) for s in block_cooldowns)
        self._consecutive_blocks = 0
        self._cooldowns_used = 0

        # NOTE: max_concurrency defaults to 1 ON PURPOSE. For the pilot we
        # send one request at a time per domain -- this is intentional
        # politeness toward small college-newspaper servers, not a technical
        # limitation. The Semaphore is retained so the pilot can be scaled up
        # later simply by raising max_concurrency in config.
        self.max_concurrency = max_concurrency
        self._semaphore = threading.Semaphore(max_concurrency)

        # Per-domain robots.txt parser cache.
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=backoff_factor,  # exponential: {backoff}*2**(n-1)
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # -- robots.txt -----------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser | None:
        """Return a cached RobotFileParser for the URL's domain.

        Returns ``None`` when robots.txt is absent (404) or could not be
        retrieved -- in both cases we fail open (allow fetching), but the two
        cases are logged differently so the distinction is auditable.
        """
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"

        with self._robots_lock:
            if domain in self._robots_cache:
                return self._robots_cache[domain]

        robots_url = f"{domain}/robots.txt"
        parser: RobotFileParser | None
        try:
            resp = self.session.get(robots_url, timeout=self.timeout)
            if resp.status_code == 404:
                logger.info("No robots.txt found for %s, proceeding.", domain)
                parser = None
            elif resp.status_code >= 400:
                logger.warning(
                    "Could not retrieve robots.txt for %s, proceeding with caution.",
                    domain,
                )
                parser = None
            else:
                parser = RobotFileParser()
                parser.parse(resp.text.splitlines())
        except requests.RequestException:
            logger.warning(
                "Could not retrieve robots.txt for %s, proceeding with caution.",
                domain,
            )
            parser = None

        with self._robots_lock:
            self._robots_cache[domain] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        """Return True if robots.txt permits fetching ``url`` for our UA."""
        parser = self._robots_for(url)
        if parser is None:
            # No robots.txt (404) or unreachable -> fail open.
            return True
        return parser.can_fetch(self.user_agent, url)

    # -- fetching -------------------------------------------------------

    def _sleep_politely(self) -> None:
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def get(self, url: str) -> requests.Response | None:
        """Fetch ``url`` politely, honoring robots.txt and concurrency limit.

        Returns the ``Response`` on success, or ``None`` if robots.txt
        disallows the URL. Raises ``SiteBlockedError`` once the site has kept
        refusing us through every cooldown.
        """
        if not self.can_fetch(url):
            logger.warning("robots.txt disallows fetching %s -- skipping.", url)
            return None

        with self._semaphore:
            self._sleep_politely()
            logger.debug("GET %s", url)
            resp = self.session.get(url, timeout=self.timeout)

        if resp.status_code in BLOCK_STATUSES:
            self._note_block(url, resp)
        elif resp.status_code < 400:
            self._consecutive_blocks = 0
            self._cooldowns_used = 0
        resp.raise_for_status()
        return resp

    def _note_block(self, url: str, resp: requests.Response) -> None:
        """Count a refusal; sleep through a cooldown or give up on the site."""
        self._consecutive_blocks += 1
        if self._consecutive_blocks < self.block_threshold:
            return
        host = urlparse(url).netloc
        if self._cooldowns_used >= len(self.block_cooldowns):
            raise SiteBlockedError(
                f"{host} kept returning HTTP {resp.status_code} after "
                f"{self._cooldowns_used} cooldown(s); stopping this site."
            )
        wait = max(
            self.block_cooldowns[self._cooldowns_used], _retry_after_seconds(resp)
        )
        self._cooldowns_used += 1
        logger.warning(
            "%s returned HTTP %d on %d consecutive request(s); cooling down "
            "%.0f min (cooldown %d/%d) before one probe request.",
            host,
            resp.status_code,
            self._consecutive_blocks,
            wait / 60,
            self._cooldowns_used,
            len(self.block_cooldowns),
        )
        time.sleep(wait)
        # A single refused probe should trigger the next cooldown, not
        # another full run of block_threshold requests.
        self._consecutive_blocks = self.block_threshold - 1

    def close(self) -> None:
        self.session.close()
