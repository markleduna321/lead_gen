from dataclasses import asdict, dataclass
import re
import unicodedata
import uuid

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from discovery_job_filters import normalize_text


JOB_SIGNAL_MAP = {
    "video_editing": [
        "video editor", "video content creator", "motion graphics designer", "video production specialist",
    ],
    "bpo_customer_service": [
        "customer service representative", "customer support agent", "call center agent", "support specialist",
    ],
    "bpo_data_entry": [
        "data entry specialist", "data processing clerk", "back office processor", "data operations analyst",
    ],
    "social_media_management": [
        "social media manager", "social media specialist", "community manager", "content creator social media",
    ],
    "virtual_assistant": [
        "virtual assistant", "executive assistant remote", "administrative assistant remote", "online business manager",
    ],
}

_BUILTIN_NAMES = {
    "video_editing": "Video Editing",
    "bpo_customer_service": "Customer Service BPO",
    "bpo_data_entry": "Data Entry BPO",
    "social_media_management": "Social Media Management",
    "virtual_assistant": "Virtual Assistant",
}


@dataclass(frozen=True)
class HiringService:
    id: str
    name: str
    roles: tuple[str, ...]
    title_phrases: tuple[str, ...]
    builtin: bool = False

    def public_dict(self) -> dict:
        return asdict(self)


BUILTIN_SERVICES = {
    key: HiringService(key, _BUILTIN_NAMES[key], tuple(roles), (), True)
    for key, roles in JOB_SIGNAL_MAP.items()
}


class DuplicateHiringService(ValueError):
    pass


def service_name_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{label} cannot contain control characters.")
    value = " ".join(value.split())
    if not 3 <= len(value) <= maximum or not normalize_text(value):
        raise ValueError(f"{label} must contain 3 to {maximum} characters.")
    return value


def _phrases(value: object, label: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{label} must be a list of 1 to {maximum} phrases.")
    result = {}
    for item in value:
        phrase = _text(item, label, 100)
        result.setdefault(normalize_text(phrase), phrase)
    return tuple(result.values())


def validate_service_payload(data: object) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if not isinstance(data, dict):
        raise ValueError("Add Service expects a JSON object.")
    name = _text(data.get("name"), "Service name", 80)
    if service_name_key(name) in {service_name_key(item.name) for item in BUILTIN_SERVICES.values()}:
        raise DuplicateHiringService("A service with that name already exists.")
    return (
        name,
        _phrases(data.get("roles"), "Job-role searches", 4),
        _phrases(data.get("title_phrases"), "Title-match phrases", 12),
    )


def list_hiring_services(dsn: str) -> list[HiringService]:
    with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT service_id, name, roles, title_phrases FROM hiring_services ORDER BY created_at, service_id;")
            rows = cur.fetchall()
    services = list(BUILTIN_SERVICES.values())
    for row in rows:
        name, roles, phrases = validate_service_payload(row)
        service_id = row["service_id"]
        if not isinstance(service_id, str) or not re.fullmatch(r"custom_[a-f0-9]{32}", service_id):
            raise ValueError("Stored hiring service has an invalid ID.")
        services.append(HiringService(service_id, name, roles, phrases))
    return services


def create_hiring_service(dsn: str, data: object) -> HiringService:
    name, roles, phrases = validate_service_payload(data)
    service = HiringService("custom_" + uuid.uuid4().hex, name, roles, phrases)
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO hiring_services (service_id, name, name_key, roles, title_phrases)
                    VALUES (%s, %s, %s, %s, %s);
                """, (service.id, name, service_name_key(name), Json(roles), Json(phrases)))
            conn.commit()
    except psycopg2.errors.UniqueViolation as error:
        raise DuplicateHiringService("A service with that name already exists.") from error
    return service


def matches_service_title(title: str, service: HiringService) -> bool:
    normalized = f" {normalize_text(title)} "
    return any(f" {normalize_text(phrase)} " in normalized for phrase in service.title_phrases)
