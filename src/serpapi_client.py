"""Shared SerpApi access with retries for transient transport failures.

Read timeouts and connection resets from the provider are common and almost
always succeed on a retry, so they are handled here rather than aborting a
whole search. Provider-reported problems (a bad key, an exhausted quota) are
*not* retried: repeating them only wastes time and quota.
"""

import time

import requests

ENDPOINT = "https://serpapi.com/search"
DEFAULT_TIMEOUT = 30
DEFAULT_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
# Rate limiting and gateway faults are worth repeating; 4xx client errors are not.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class SerpApiUnavailable(RuntimeError):
    """The provider could not be reached after every attempt."""


def describe(error: BaseException) -> str:
    """A short, human-readable cause.

    requests' own text ("HTTPSConnectionPool(host=..., port=443): Read timed
    out. (read timeout=10)") is noise in an operator-facing log.
    """
    if isinstance(error, requests.Timeout):
        return "the search provider timed out"
    if isinstance(error, requests.ConnectionError):
        return "the search provider could not be reached"
    return str(error) or error.__class__.__name__


def fetch(params: dict, timeout: int = DEFAULT_TIMEOUT, attempts: int = DEFAULT_ATTEMPTS,
          log=None, label: str = "search"):
    """GETs the SerpApi endpoint, retrying transient failures with backoff."""
    attempts = max(1, attempts)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(ENDPOINT, params=params, timeout=timeout)
            if response.status_code in RETRY_STATUS:
                raise SerpApiUnavailable(f"provider returned HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError, SerpApiUnavailable) as error:
            last_error = error
            if attempt == attempts:
                break
            delay = BACKOFF_SECONDS * (2 ** (attempt - 1))
            if log:
                log(f"[PROVIDER RETRY] {label}: {describe(error)} "
                    f"(attempt {attempt} of {attempts}); retrying in {delay:.0f}s.")
            time.sleep(delay)
    raise SerpApiUnavailable(
        f"{label}: {describe(last_error)} after {attempts} attempts."
    ) from last_error
