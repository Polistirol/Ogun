"""RemoteOK worker: public JSON, software/AI filter, normalization.

Endpoint: https://remoteok.com/api
The first array element is a legal notice, not a listing.
The API accepts `?tags=python,ai` but the tag vocabulary is closed and
AND/OR semantics are undocumented: one GET of the dump, client-side OR
filter on title + tags (one request, no bursts).
"""

from __future__ import annotations

from typing import Any, Sequence

from scrapers._common import (
    clean_location,
    dumps_raw,
    html_to_text,
    http_get,
    iso_date,
    matches_keywords,
    parse_keywords,
)

SOURCE = "remoteok"
API_URL = "https://remoteok.com/api"


def _is_job(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("position") or item.get("id"))


def _tags(item: dict[str, Any]) -> list[str]:
    raw = item.get("tags") or []
    if isinstance(raw, str):
        return [raw]
    return [str(t) for t in raw if t]


def normalize_job(item: dict[str, Any]) -> dict[str, Any] | None:
    title = str(item.get("position") or "").strip()
    url = str(item.get("url") or item.get("apply_url") or "").strip()
    if not title or not url:
        return None
    location = clean_location(item.get("location"))
    company = str(item.get("company") or "").strip() or None
    description = html_to_text(item.get("description"))
    return {
        "source": SOURCE,
        "title": title,
        "company": company,
        "url": url,
        "description": description or None,
        "location": location,
        "remote": 1,
        "posted_date": iso_date(item.get("date")),
        "raw_json": dumps_raw(item),
    }


def fetch_jobs(keywords: Sequence[str] | str | None = None) -> list[dict[str, Any]]:
    """Download the public dump and return normalized, filtered listings."""
    kws = parse_keywords(keywords)
    response = http_get(
        API_URL,
        headers={"Accept": "application/json"},
    )
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"RemoteOK: unexpected response ({type(payload).__name__})")

    jobs: list[dict[str, Any]] = []
    for item in payload:
        if not _is_job(item):
            continue
        tags = _tags(item)
        title = str(item.get("position") or "")
        if not matches_keywords(kws, title=title, tags=tags):
            continue
        normalized = normalize_job(item)
        if normalized:
            jobs.append(normalized)
    return jobs
