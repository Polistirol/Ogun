"""We Work Remotely worker: public Programming RSS feed.

Feed: https://weworkremotely.com/categories/remote-programming-jobs.rss
No keyword query string: client-side filter on title + category.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Sequence

from scrapers._common import (
    clean_location,
    dumps_raw,
    html_to_text,
    http_get,
    matches_keywords,
    parse_keywords,
)

SOURCE = "weworkremotely"
RSS_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"


def _child_text(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _split_title(raw_title: str) -> tuple[str | None, str]:
    """WWR titles are 'Company: Role'. If the separator is missing, the whole string is the title."""
    text = (raw_title or "").strip()
    if ": " in text:
        company, title = text.split(": ", 1)
        return (company.strip() or None), (title.strip() or text)
    return None, text


def _posted_date(pub_date: str) -> str | None:
    if not pub_date:
        return None
    try:
        return parsedate_to_datetime(pub_date).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _location(item: ET.Element, description_text: str) -> str | None:
    region = clean_location(_child_text(item, "region"))
    if region:
        return region
    for line in description_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("headquarters:"):
            return clean_location(stripped.split(":", 1)[1])
    return None


def normalize_item(item: ET.Element) -> dict[str, Any] | None:
    raw_title = _child_text(item, "title")
    url = _child_text(item, "link") or _child_text(item, "guid")
    if not raw_title or not url:
        return None
    company, title = _split_title(raw_title)
    description_html = _child_text(item, "description")
    description = html_to_text(description_html)
    category = _child_text(item, "category")
    pub_date = _child_text(item, "pubDate")
    location = _location(item, description)
    raw = {
        "title": raw_title,
        "company": company,
        "link": url,
        "guid": _child_text(item, "guid"),
        "pubDate": pub_date,
        "region": _child_text(item, "region"),
        "category": category,
        "description": description_html,
    }
    return {
        "source": SOURCE,
        "title": title,
        "company": company,
        "url": url,
        "description": description or None,
        "location": location,
        "remote": 1,
        "posted_date": _posted_date(pub_date),
        "raw_json": dumps_raw(raw),
        "_category": category,
    }


def fetch_jobs(keywords: Sequence[str] | str | None = None) -> list[dict[str, Any]]:
    """Download the Programming RSS and return normalized, filtered listings."""
    kws = parse_keywords(keywords)
    response = http_get(
        RSS_URL,
        headers={"Accept": "application/rss+xml, application/xml, text/xml"},
    )
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise ValueError(f"We Work Remotely: invalid RSS ({exc})") from exc

    jobs: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        normalized = normalize_item(item)
        if not normalized:
            continue
        category = str(normalized.pop("_category", "") or "")
        tags = [category] if category else []
        if not matches_keywords(kws, title=normalized["title"], tags=tags):
            continue
        jobs.append(normalized)
    return jobs
