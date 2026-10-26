"""Shared Scrapingdog API client with retries for transient transport failures.

Handles routing to Scrapingdog's specialized endpoints (Google Search,
Google Maps, Google Jobs, Yelp) while retrying transient errors (429 rate
limits, 5xx server issues, timeouts, connection drops) with exponential backoff.
Client-side errors (401 invalid key, 400 bad request) fail immediately to
avoid burning quotas.
"""

import os
import time
from collections.abc import Mapping
import requests

SCRAPINGDOG_BASE_URL = "https://api.scrapingdog.com"
SCRAPINGDOG_GOOGLE_ENDPOINT = f"{SCRAPINGDOG_BASE_URL}/google"
SCRAPINGDOG_MAPS_ENDPOINT = f"{SCRAPINGDOG_BASE_URL}/google_maps"
SCRAPINGDOG_JOBS_ENDPOINT = f"{SCRAPINGDOG_BASE_URL}/google_jobs"
SCRAPINGDOG_YELP_ENDPOINT = f"{SCRAPINGDOG_BASE_URL}/yelp/search"

SERPAPI_ENDPOINT = "https://serpapi.com/search"

DEFAULT_TIMEOUT = 30
DEFAULT_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
# Rate limiting and gateway faults are worth repeating; 4xx client errors are not.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class ScrapingdogUnavailable(RuntimeError):
    """The provider could not be reached after every attempt."""


# Alias for backward compatibility with existing handlers
SerpApiUnavailable = ScrapingdogUnavailable


def describe(error: BaseException) -> str:
    """A short, human-readable cause for operator-facing logs."""
    if isinstance(error, requests.Timeout):
        return "the search provider timed out"
    if isinstance(error, requests.ConnectionError):
        return "the search provider could not be reached"
    return str(error) or error.__class__.__name__


def resolve_endpoint_and_params(params: Mapping[str, object], endpoint: str | None = None) -> tuple[str, dict]:
    """Resolves target Scrapingdog endpoint and maps parameters from query requests."""
    req_params = dict(params)

    # If the caller specifically asked for an explicit endpoint (e.g. SerpApi in legacy tests), honor it
    if endpoint and "serpapi.com" in endpoint:
        return endpoint, req_params

    engine = req_params.get("engine")

    if endpoint:
        target_url = endpoint
    elif engine == "google_maps":
        target_url = SCRAPINGDOG_MAPS_ENDPOINT
    elif engine == "google_jobs":
        target_url = SCRAPINGDOG_JOBS_ENDPOINT
    elif engine == "yelp":
        target_url = SCRAPINGDOG_YELP_ENDPOINT
    else:
        # Default / google search
        target_url = SCRAPINGDOG_GOOGLE_ENDPOINT

    # Map parameters so both Scrapingdog and legacy parameter names are available
    query = req_params.get("query") or req_params.get("q")
    if query:
        req_params["query"] = query
        req_params["q"] = query

    if target_url == SCRAPINGDOG_JOBS_ENDPOINT or engine == "google_jobs":
        country = req_params.get("country") or req_params.get("gl")
        if country:
            req_params["country"] = country
            req_params["gl"] = country

    if target_url == SCRAPINGDOG_YELP_ENDPOINT or engine == "yelp":
        desc = req_params.get("find_desc") or query
        if desc:
            req_params["find_desc"] = desc
        loc = req_params.get("find_loc") or req_params.get("location")
        if loc:
            req_params["find_loc"] = loc
            req_params["location"] = loc

    return target_url, req_params


def fetch(params: dict, timeout: int = DEFAULT_TIMEOUT, attempts: int = DEFAULT_ATTEMPTS,
          log=None, label: str = "search", endpoint: str | None = None):
    """GETs the Scrapingdog endpoint, retrying transient failures with backoff."""
    url, req_params = resolve_endpoint_and_params(params, endpoint=endpoint)
    attempts = max(1, attempts)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=req_params, timeout=timeout)
            if response.status_code in RETRY_STATUS:
                raise ScrapingdogUnavailable(f"provider returned HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError, ScrapingdogUnavailable) as error:
            last_error = error
            if attempt == attempts:
                break
            delay = BACKOFF_SECONDS * (2 ** (attempt - 1))
            if log:
                log(f"[PROVIDER RETRY] {label}: {describe(error)} "
                    f"(attempt {attempt} of {attempts}); retrying in {delay:.0f}s.")
            time.sleep(delay)
    raise ScrapingdogUnavailable(
        f"{label}: {describe(last_error)} after {attempts} attempts."
    ) from last_error
