"""Deterministic job identity helpers; never fetch network content."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid"}
MAX_JOB_TEXT_LENGTH = 50_000


def normalize_job_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Job URL must use HTTP or HTTPS.")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    return urlunsplit((scheme, host, parsed.path or "/", urlencode(sorted(query)), ""))


def normalized_text(value: str) -> str:
    return " ".join(value.split())


def job_content_hash(cleaned_job_description: str) -> str:
    cleaned = normalized_text(cleaned_job_description)
    if not cleaned:
        raise ValueError("cleaned_job_description must not be blank.")
    if len(cleaned_job_description) > MAX_JOB_TEXT_LENGTH:
        raise ValueError("Job description exceeds 50,000 characters.")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def validate_extracted_page_text(value: str | None) -> None:
    if value is None:
        return
    if len(value) > MAX_JOB_TEXT_LENGTH:
        raise ValueError("Raw page text exceeds 50,000 characters.")
    lowered = value.lower()
    if "\x00" in value or "<script" in lowered or "javascript:" in lowered:
        raise ValueError("Raw page content must be extracted inert text, not executable content.")
