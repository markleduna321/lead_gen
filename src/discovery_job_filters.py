import re
import unicodedata
from dataclasses import dataclass

import pycountry


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"\w+", "".join(
        character for character in text if not unicodedata.combining(character)
    )))


_COUNTRY_ALIASES = {
    "us": ("USA", "U.S.", "U.S.A.", "United States of America"),
    "gb": ("UK", "U.K.", "Great Britain", "Britain", "England", "Scotland", "Wales", "Northern Ireland"),
    "ae": ("UAE", "U.A.E."),
    "kr": ("South Korea",),
    "kp": ("North Korea",),
    "ru": ("Russia",),
    "vn": ("Vietnam",),
    "tw": ("Taiwan",),
    "tz": ("Tanzania",),
    "bo": ("Bolivia",),
    "ve": ("Venezuela",),
    "ir": ("Iran",),
    "la": ("Laos",),
    "sy": ("Syria",),
    "md": ("Moldova",),
    "ps": ("Palestine",),
    "cd": ("DR Congo", "Democratic Republic of the Congo"),
}


@dataclass(frozen=True)
class HiringCountry:
    code: str
    name: str
    aliases: tuple[str, ...]


def resolve_hiring_country(value: str) -> HiringCountry | None:
    if not isinstance(value, str):
        raise ValueError("Country must be text or left blank.")
    value = value.strip()
    if not value:
        return None
    normalized = normalize_text(value)
    alias_code = next((
        code for code, aliases in _COUNTRY_ALIASES.items()
        if normalized in {normalize_text(alias) for alias in aliases}
    ), None)
    try:
        country = pycountry.countries.lookup(alias_code or value)
    except LookupError as error:
        raise ValueError("Country not recognized. Enter a country name or ISO country code, or leave it blank.") from error
    code = country.alpha_2.lower()
    aliases = {
        country.name, country.alpha_3,
        *_COUNTRY_ALIASES.get(code, ()),
    }
    for field in ("official_name", "common_name"):
        name = getattr(country, field, None)
        if name:
            aliases.add(name)
    # Two-letter codes can be ambiguous regions (CA is also California).
    if code == "us":
        aliases.add("US")
    return HiringCountry(code, country.name, tuple(sorted(aliases)))


_ROLE_PATTERNS = {
    "video_editing": (
        r"video (?:content )?(?:editor|creator|producer|production specialist)s?",
        r"video editing (?:specialist|assistant)s?",
        r"motion (?:graphics? )?(?:designer|artist)s?",
        r"editor video",
    ),
    "bpo_customer_service": (
        r"customer (?:service|support|care)(?: representatives?| agents?| specialists?| associates?)",
        r"(?:call|contact) cent(?:er|re)(?: agents?| representatives?| specialists?)",
        r"support (?:specialist|agent|representative)s?",
    ),
    "bpo_data_entry": (
        r"data entry(?: specialist|clerk|operator)?s?",
        r"data processing(?: clerk|specialist|operator)?s?",
        r"back office processor(?:s)?",
        r"data operations analyst(?:s)?",
    ),
    "social_media_management": (
        r"social media (?:manager|specialist|coordinator|strategist|content creator)s?",
        r"community manager(?:s)?",
        r"content creator social media",
    ),
    "virtual_assistant": (
        r"(?:virtual|executive|administrative|personal) assistant(?:s)?",
        r"online business manager(?:s)?",
    ),
}


def matches_hiring_role(title: str, service_type: str) -> bool:
    patterns = _ROLE_PATTERNS.get(service_type)
    if patterns is None:
        raise ValueError(f"Unsupported hiring service: {service_type}")
    normalized = normalize_text(title)
    return any(re.search(rf"\b(?:{pattern})\b", normalized) for pattern in patterns)


def matches_hiring_country(location: str, country: HiringCountry | None) -> bool:
    if country is None:
        return True
    # Use the returned location, never a headquarters/benefits mention in the description.
    segments = re.split(r"[,;|/()\-\u2013\u2014]", location)
    segments.append(location)
    aliases = {normalize_text(alias) for alias in country.aliases}
    for segment in segments:
        normalized = normalize_text(segment)
        normalized = re.sub(r"^(?:remote|work from home)(?: in| within| across)? ", "", normalized)
        normalized = re.sub(r" (?:remote|only)$", "", normalized)
        if normalized in aliases:
            return True
    return False
