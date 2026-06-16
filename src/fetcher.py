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
    ) -> None:
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.timeout = timeout
        self.user_agent = user_agent

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
        disallows the URL.
        """
        if not self.can_fetch(url):
            logger.warning("robots.txt disallows fetching %s -- skipping.", url)
            return None

        with self._semaphore:
            self._sleep_politely()
            logger.debug("GET %s", url)
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp

    def close(self) -> None:
        self.session.close()
