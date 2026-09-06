"""Shared utilities for scraping workers (no LLM)."""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any, Iterable, Mapping, Sequence

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36 "
    "(OgunJob/0.1; personal job-search tool)"
)

# Default list: software/AI roles. Overridable from CLI (--keywords).
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "python",
    "ai",
    "ml",
    "llm",
    "backend",
    "software",
    "agent",
    "machine learning",
    "developer",
    "engineer",
    "full-stack",
    "fullstack",
    "frontend",
    "front-end",
    "devops",
    "data",
    "nlp",
    "gpt",
)

_RETRY_STATUSES = {429, 500, 502, 503, 504}


def parse_keywords(value: str | Sequence[str] | None) -> list[str]:
    """Accept 'python,ai,agent' or a sequence; empty → default."""
    if value is None:
        return list(DEFAULT_KEYWORDS)
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
        return parts or list(DEFAULT_KEYWORDS)
    parts = [str(p).strip().lower() for p in value if str(p).strip()]
    return parts or list(DEFAULT_KEYWORDS)


# Alone ("Customer Service Agent") they are not enough if software keywords are also present.
AMBIGUOUS_KEYWORDS = frozenset({"agent"})


def matches_keywords(
    keywords: Sequence[str],
    *,
    title: str = "",
    tags: Iterable[str] | None = None,
) -> bool:
    """True se almeno una keyword compare nel titolo (parola intera) o nei tag."""
    if not keywords:
        return True
    title_l = (title or "").lower()
    tagset = {(t or "").strip().lower() for t in (tags or []) if t}
    provided = {(k or "").strip().lower() for k in keywords if (k or "").strip()}
    strong: set[str] = set()
    weak: set[str] = set()
    for kw in provided:
        if kw in tagset:
            strong.add(kw)
            continue
        if kw == "agent":
            if re.search(r"\bagentic\b", title_l):
                strong.add(kw)
            elif re.search(r"\bagents?\b", title_l):
                weak.add(kw)
            continue
        if re.search(rf"\b{re.escape(kw)}\b", title_l):
            strong.add(kw)
    if strong:
        return True
    if provided <= AMBIGUOUS_KEYWORDS:
        return bool(weak)
    return False


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_location(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"[\s,]+$", "", str(value).strip())
    return text or None


def iso_date(value: str | None) -> str | None:
    """Riduce un datetime ISO (con o senza tz) a YYYY-MM-DD; None se vuoto."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    return text[:10] or None


def dumps_raw(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def http_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    retries: int = 3,
    retry_delay: float = 1.5,
) -> requests.Response:
    """GET con User-Agent, retry e backoff su 429/5xx e errori di rete."""
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=merged, timeout=timeout)
            if response.status_code in _RETRY_STATUSES:
                last_error = requests.HTTPError(
                    f"{response.status_code} {response.reason} for {url}",
                    response=response,
                )
                time.sleep(retry_delay * (attempt + 1))
                continue
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            time.sleep(retry_delay * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError(f"GET fallita senza errore esplicito: {url}")
