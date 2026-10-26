"""Backward-compatible SerpApi adapter transitioning to Scrapingdog.

All search operations have transitioned to Scrapingdog as the primary provider.
This module re-exports the Scrapingdog client interface for backward compatibility.
"""

import time
import requests
import scrapingdog_client
from scrapingdog_client import (
    SCRAPINGDOG_BASE_URL,
    SCRAPINGDOG_GOOGLE_ENDPOINT,
    SCRAPINGDOG_MAPS_ENDPOINT,
    SCRAPINGDOG_JOBS_ENDPOINT,
    SCRAPINGDOG_YELP_ENDPOINT,
    DEFAULT_TIMEOUT,
    DEFAULT_ATTEMPTS,
    BACKOFF_SECONDS,
    RETRY_STATUS,
    ScrapingdogUnavailable,
    SerpApiUnavailable,
    describe,
    resolve_endpoint_and_params,
)

ENDPOINT = "https://serpapi.com/search"


def fetch(params: dict, timeout: int = DEFAULT_TIMEOUT, attempts: int = DEFAULT_ATTEMPTS,
          log=None, label: str = "search", endpoint: str | None = None):
    """GETs Scrapingdog endpoint, retrying transient failures with backoff."""
    return scrapingdog_client.fetch(
        params=params,
        timeout=timeout,
        attempts=attempts,
        log=log,
        label=label,
        endpoint=endpoint,
    )
