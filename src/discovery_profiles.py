import re
import uuid
from urllib.parse import parse_qs, urlsplit

import psycopg2


def canonical_social_profile(source: str, url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {
        "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com"},
        "instagram": {"instagram.com", "www.instagram.com", "m.instagram.com"},
    }
    if source == "linkedin":
        if (
            not host.endswith(".linkedin.com") and host != "linkedin.com"
        ) or parsed.scheme not in {"http", "https"}:
            raise ValueError("Expected a LinkedIn company page URL")
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            len(segments) != 2 or segments[0].lower() != "company"
            or not re.fullmatch(r"[A-Za-z0-9._-]+", segments[1])
        ):
            raise ValueError("Search result is not a LinkedIn company page")
        canonical = f"https://www.linkedin.com/company/{segments[1].lower()}"
        return "ln_serp_" + uuid.uuid5(uuid.NAMESPACE_URL, canonical).hex, canonical

    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts.get(source, set()):
        raise ValueError("Expected a supported Facebook or Instagram profile URL")
    path = parsed.path.strip("/").lower()
    if source == "facebook" and path == "profile.php":
        identity = parse_qs(parsed.query).get("id", [""])[0]
        if not identity.isdigit():
            raise ValueError("Facebook profile.php URL is missing a numeric id")
        path = f"profile.php?id={identity}"
    elif source == "facebook" and (
        re.fullmatch(r"(?:pages|people)/[^/]+/[0-9]+", path)
    ):
        path = f"profile.php?id={path.rsplit('/', 1)[1]}"
    elif not re.fullmatch(r"[a-z0-9._-]+", path) or path in {
        "p", "reel", "reels", "explore", "stories", "accounts", "direct", "groups",
        "watch", "marketplace", "events", "login", "search", "share", "pages", "people",
    }:
        raise ValueError("Search result is not a business profile")
    canonical = f"https://{source}.com/{path}"
    prefix = "fb_serp_" if source == "facebook" else "ig_serp_"
    return prefix + uuid.uuid5(uuid.NAMESPACE_URL, canonical).hex, canonical


def load_existing_social_ids(dsn: str | None, source: str, user_id: int | None = None) -> set[str]:
    """Profiles this account already holds.

    Scoped per account: an unscoped read would let one user's saved profiles
    silently suppress another user's discovery results.
    """
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT google_place_id, profile_url FROM prospects WHERE user_id = %s;",
                (user_id,),
            )
            rows = cur.fetchall()
    identities = set()
    for place_id, profile_url in rows:
        if place_id:
            identities.add(place_id)
        if profile_url:
            try:
                identity, _ = canonical_social_profile(source, profile_url)
            except ValueError:
                continue  # Other sources and legacy non-profile links do not identify this source.
            identities.add(identity)
    return identities
