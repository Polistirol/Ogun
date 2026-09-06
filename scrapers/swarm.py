"""Orchestratore dello swarm di scraper (parallelizzazione, non agenti LLM).

Uso:
    python -m scrapers.swarm
    python -m scrapers.swarm --keywords python,ai,agent
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

from scrapers._common import parse_keywords
from scrapers.remoteok import SOURCE as REMOTEOK_SOURCE
from scrapers.remoteok import fetch_jobs as fetch_remoteok
from scrapers.weworkremotely import SOURCE as WWR_SOURCE
from scrapers.weworkremotely import fetch_jobs as fetch_wwr

DB_PATH = Path(__file__).resolve().parent.parent / "ogunjob.db"

# Faithful copy of schema.sql (jobs table + indexes). Do not invent columns.
JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    url TEXT NOT NULL,
    description TEXT,
    location TEXT,
    remote INTEGER NOT NULL DEFAULT 0,
    posted_date TEXT,
    scraped_at TEXT NOT NULL,
    raw_json TEXT,
    UNIQUE (source, url)
);

CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs (source);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs (scraped_at);
"""

FetchFn = Callable[[Sequence[str]], list[dict[str, Any]]]

WORKERS: tuple[tuple[str, FetchFn], ...] = (
    (REMOTEOK_SOURCE, fetch_remoteok),
    (WWR_SOURCE, fetch_wwr),
)


def _normalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _title_company_hash(title: str, company: str | None) -> str:
    key = f"{(title or '').strip().lower()}|{(company or '').strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def deduplicate(jobs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same normalized URL or same title+company hash → one listing."""
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    unique: list[dict[str, Any]] = []
    for job in jobs:
        url_key = _normalize_url(str(job.get("url") or ""))
        hash_key = _title_company_hash(str(job.get("title") or ""), job.get("company"))
        if url_key and url_key in seen_urls:
            continue
        if hash_key in seen_hashes:
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_hashes.add(hash_key)
        unique.append(job)
    return unique


async def _run_worker(
    name: str, fetch_fn: FetchFn, keywords: Sequence[str]
) -> tuple[str, list[dict[str, Any]], str | None]:
    try:
        jobs = await asyncio.to_thread(fetch_fn, keywords)
        return name, jobs, None
    except Exception as exc:
        traceback.print_exc()
        return name, [], f"{type(exc).__name__}: {exc}"


async def run_swarm(keywords: Sequence[str]) -> dict[str, Any]:
    """Run workers in parallel; one failure does not stop the other sources."""
    gathered = await asyncio.gather(
        *(_run_worker(name, fn, keywords) for name, fn in WORKERS)
    )
    per_source: dict[str, int] = {}
    errors: dict[str, str] = {}
    combined: list[dict[str, Any]] = []
    for name, jobs, error in gathered:
        per_source[name] = len(jobs)
        if error:
            errors[name] = error
        combined.extend(jobs)
    unique = deduplicate(combined)
    return {
        "per_source": per_source,
        "errors": errors,
        "raw_count": len(combined),
        "deduped": unique,
    }


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(JOBS_SCHEMA)
    return conn


def write_jobs(jobs: Sequence[dict[str, Any]]) -> int:
    """INSERT OR IGNORE on UNIQUE(source, url). Returns new rows."""
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.executemany(
            """
            INSERT OR IGNORE INTO jobs (
                source, title, company, url, description, location,
                remote, posted_date, scraped_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    job["source"],
                    job["title"],
                    job.get("company"),
                    job["url"],
                    job.get("description"),
                    job.get("location"),
                    int(job.get("remote") or 0),
                    job.get("posted_date"),
                    scraped_at,
                    job.get("raw_json"),
                )
                for job in jobs
            ],
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return after - before
    finally:
        conn.close()


def _print_summary(
    keywords: Sequence[str],
    result: dict[str, Any],
    written: int,
) -> None:
    print(f"Keywords: {', '.join(keywords)}")
    print(f"DB: {DB_PATH}")
    print()
    for name, _fn in WORKERS:
        count = result["per_source"].get(name, 0)
        error = result["errors"].get(name)
        if error:
            print(f"  {name}: ERROR — {error}")
        else:
            print(f"  {name}: {count} listings")
    print()
    print(f"Found (sum of sources): {result['raw_count']}")
    print(f"After dedup:            {len(result['deduped'])}")
    print(f"Written to DB:          {written}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scrapers.swarm",
        description="Parallel scraper swarm into ogunjob.db (no LLM).",
    )
    parser.add_argument(
        "--keywords",
        default=None,
        help=(
            "Comma-separated list (filter on title/tags). "
            "Default: python,ai,ml,llm,backend,software,agent,..."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    keywords = parse_keywords(args.keywords)
    result = asyncio.run(run_swarm(keywords))
    written = write_jobs(result["deduped"])
    _print_summary(keywords, result, written)
    return 1 if result["errors"] and not result["deduped"] else 0


if __name__ == "__main__":
    sys.exit(main())
