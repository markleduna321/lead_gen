"""Private Cloudflare R2 (S3-compatible) storage for website screenshots.

Objects are never public: the app checks ownership, then hands the browser a
short-lived signed link.
"""

import os
import re
import threading
import uuid

REQUIRED_SETTINGS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
SIGNED_URL_SECONDS = 300
_KEY_PATTERN = re.compile(r"^screenshots/[0-9a-f]{32}\.png$")

_client = None
_client_settings = None
_lock = threading.Lock()


def configured() -> bool:
    return all(os.getenv(name) for name in REQUIRED_SETTINGS)


def is_screenshot_key(value) -> bool:
    return isinstance(value, str) and bool(_KEY_PATTERN.match(value))


def new_screenshot_key() -> str:
    return f"screenshots/{uuid.uuid4().hex}.png"


def _bucket() -> str:
    return os.environ["R2_BUCKET"]


def client():
    global _client, _client_settings
    settings = tuple(os.getenv(name) for name in REQUIRED_SETTINGS)
    if not all(settings):
        raise RuntimeError("Cloudflare R2 storage is not configured.")
    with _lock:
        if _client is None or _client_settings != settings:
            import boto3
            from botocore.config import Config
            account_id, access_key, secret_key, _ = settings
            _client = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name="auto",
                config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=20,
                              retries={"max_attempts": 3}),
            )
            _client_settings = settings
        return _client


def upload_png(data: bytes, key: str | None = None) -> str:
    key = key or new_screenshot_key()
    if not is_screenshot_key(key):
        raise ValueError("Invalid screenshot key.")
    client().put_object(Bucket=_bucket(), Key=key, Body=data, ContentType="image/png",
                        CacheControl="private, max-age=300")
    return key


def signed_url(key: str, expires: int = SIGNED_URL_SECONDS) -> str:
    if not is_screenshot_key(key):
        raise ValueError("Invalid screenshot key.")
    return client().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": key, "ResponseContentType": "image/png"},
        ExpiresIn=expires,
    )
