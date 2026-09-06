#!/usr/bin/env python3
"""
matcher/matcher.py
------------------
Compute a fit score (0.0–1.0) between the candidate profile and the
listings in ogunjob.db, and write it to the matches table.

Scoring: deterministic lexical overlap + batched LLM.
The LLM may recognize synonyms, but cannot invent skills absent from
the profile: the final score is capped by lexical evidence.
If the LLM is unavailable, only the lexical component remains.

Usage:
    python -m matcher.matcher
    python -m matcher.matcher --force
    python -m matcher.matcher --top 10
    python -m matcher.matcher --provider lmstudio --limit 8
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import yaml

from llm_provider import (
    SUPPORTED_PROVIDERS,
    get_llm_client,
    log_llm_start,
    log_llm_usage,
    parse_json_object,
)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "ogunjob.db"
DEFAULT_CV_MASTER = ROOT / "config" / "cv_master.yaml"
DEFAULT_PROFILE_MD = ROOT / "memory" / "profile.md"

# Faithful copy of schema.sql (skills + matches + index). Do not invent columns.
SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    tags TEXT,
    source TEXT,
    confidence REAL,
    UNIQUE (label, source)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    score REAL NOT NULL,
    rationale TEXT,
    computed_at TEXT NOT NULL,
    UNIQUE (job_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_score ON matches (score DESC);
"""

# L'LLM puo' premiare sinonimi, ma non superare l'evidenza lessicale di tanto.
LLM_HEADROOM = 0.35
DEFAULT_BATCH_SIZE = 5
DESC_LIMIT = 1800
DEFAULT_TOP = 5
RATIONALE_DISPLAY = 160

_PLACEHOLDER = re.compile(
    r"AGGIUNGI|AGGIORNA|SOSTITUISCI|SPECIFICA|DESCRIVI|placeholder|\bTODO\b|\bTBD\b",
    re.IGNORECASE,
)
_SPLIT_PARTS = re.compile(r"[/(),;]+")
_KEEP_SHORT = {"ai", "ml", "js", "ts", "go", "c++", "c#", "r", "3d", "qa"}
# Tokens that are too generic: they do not become aliases (otherwise "engineering" matches every listing).
_GENERIC_TOKENS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "for", "with", "to", "da",
    "di", "del", "della", "dei", "delle", "e", "ed", "un", "una", "il", "la",
    "lo", "gli", "le", "ecc", "etc", "se", "applicabile", "other", "use",
    "engineering", "engineer", "software", "systems", "system", "sistemi",
    "sistema", "tool", "tools", "apis", "api", "experience", "development",
}

# Sinonimi di skill gia' attestate — non introducono competenze nuove.
_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "typescript": ("typescript", "ts"),
    "javascript": ("javascript", "js"),
    "llm": ("llm", "llms", "large language model", "large language models"),
    "anthropic": ("anthropic", "claude"),
    "openai": ("openai", "gpt", "chatgpt"),
    "prompt": ("prompt engineering", "prompting"),
    "agenti": ("agenti", "agent", "agents", "agentic", "tool use",
               "function calling", "function-calling"),
    "robotics": ("robotics", "robotica"),
    "backend": ("backend", "back-end", "back end"),
    "laser": ("laser 3d", "3d laser", "laser"),
}

SYSTEM_PROMPT = """\
You are a candidate/listing fit evaluator. You receive:
1. The candidate's REAL profile (attested skills and experience).
2. A batch of job listings, each with a numeric job_id.

FUNDAMENTAL RULE: score ONLY on skills and experience explicitly \
present in the profile. Do not invent, do not infer undeclared stacks, \
do not inflate the score. Placeholders like AGGIUNGI/TODO in the profile \
are NOT skills. If the listing requires X and X is not in the profile, \
that is a gap: lower the score. Synonyms of an attested skill (e.g. "LLM" \
for "LLM APIs") are allowed; new skills are not.

Score is a real number between 0.0 and 1.0 (not 0-100):
- 0.0–0.25: poor fit (distant role/stack, or blocking gaps)
- 0.25–0.50: partial fit (some overlap, important gaps)
- 0.50–0.75: good fit (stack/experience cover the core of the role)
- 0.75–1.0: strong fit (only if the profile truly covers the central requirements)

You are also given a lexical_overlap (0.0–1.0) computed deterministically \
on attested skills: use it as an anchor, not as the final grade. You may \
deviate for synonyms/seniority, but if overlap is ~0 do not assign high scores.

Reply with a JSON object ONLY, nothing else:
{
  "matches": [
    {
      "job_id": 1,
      "score": 0.62,
      "rationale": "one or two honest sentences",
      "overlap": ["attested skills that cover the listing"],
      "gaps": ["listing requirements absent from the profile"]
    }
  ]
}
You must return EXACTLY one element per listing in the batch, \
same job_id.
"""


@dataclass
class AttestedSkill:
    label: str
    category: str
    aliases: tuple[str, ...]


@dataclass
class Profile:
    cv_master: dict
    profile_md: str
    skills: list[AttestedSkill]
    profile_text: str


@dataclass
class Job:
    id: int
    source: str
    title: str
    company: str
    url: str
    description: str
    location: str
    remote: int


@dataclass
class MatchResult:
    job_id: int
    score: float
    rationale: str
    overlap: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    lexical: float = 0.0
    method: str = "lexical"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def is_placeholder(text: str | None) -> bool:
    if not text or not str(text).strip():
        return True
    return bool(_PLACEHOLDER.search(str(text)))


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _aliases_for(label: str) -> tuple[str, ...]:
    """Alias = full label + parts inside /() + known synonyms. No generic tokens."""
    raw = (label or "").strip()
    if not raw:
        return ()
    aliases: set[str] = {raw.lower()}
    parts = [raw]
    parts.extend(_SPLIT_PARTS.split(raw))
    for part in parts:
        part = part.strip().lower()
        if not part:
            continue
        if part not in _GENERIC_TOKENS:
            aliases.add(part)
        for token in re.split(r"[\s+\-]+", part):
            token = token.strip()
            if not token or token in _GENERIC_TOKENS:
                continue
            extra = _ALIAS_MAP.get(token)
            if extra:
                aliases.update(extra)
            elif len(token) >= 3 or token in _KEEP_SHORT:
                aliases.add(token)
    return tuple(sorted(aliases, key=len, reverse=True))


def _collect_skill_labels(cv: dict) -> list[AttestedSkill]:
    skills_node = cv.get("skills") or {}
    out: list[AttestedSkill] = []
    seen: set[str] = set()
    if isinstance(skills_node, dict):
        for category, values in skills_node.items():
            if not isinstance(values, (list, tuple)):
                values = [values]
            for value in values:
                label = str(value or "").strip()
                if is_placeholder(label):
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    AttestedSkill(
                        label=label,
                        category=str(category),
                        aliases=_aliases_for(label),
                    )
                )
    return out


def _experience_lines(cv: dict) -> list[str]:
    lines: list[str] = []
    for exp in cv.get("experiences") or []:
        if not isinstance(exp, dict):
            continue
        company = str(exp.get("company") or "").strip()
        role = str(exp.get("role") or "").strip()
        period = str(exp.get("period") or "").strip()
        header_bits = [b for b in (role, company) if b and not is_placeholder(b)]
        if not header_bits:
            continue
        header = " — ".join(header_bits)
        if period and not is_placeholder(period):
            header += f" ({period})"
        lines.append(header)
        for block in exp.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            text = str(block.get("text") or "").strip()
            if not text or is_placeholder(text):
                continue
            # taglia al primo marker di placeholder residuo
            cut = _PLACEHOLDER.search(text)
            if cut and cut.start() < 40:
                continue
            if cut:
                text = text[: cut.start()].rstrip(" (,-")
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                lines.append(f"  - {text}")
    return lines


def build_profile_text(cv: dict, profile_md: str, skills: Sequence[AttestedSkill]) -> str:
    parts: list[str] = []
    candidate = cv.get("candidate") or {}
    if isinstance(candidate, dict):
        name = candidate.get("name")
        location = candidate.get("location")
        if name and not is_placeholder(str(name)):
            parts.append(f"Nome: {name}")
        if location and not is_placeholder(str(location)):
            parts.append(f"Localita: {location}")
        headlines = candidate.get("headline_variants") or {}
        if isinstance(headlines, dict):
            cleaned = [
                f"- {k}: {v.strip()}"
                for k, v in headlines.items()
                if isinstance(v, str) and v.strip() and not is_placeholder(v)
            ]
            if cleaned:
                parts.append("Headline dichiarate:\n" + "\n".join(cleaned))

    summaries = cv.get("summary_variants") or {}
    if isinstance(summaries, dict):
        cleaned_sum = [
            f"### {k}\n{v.strip()}"
            for k, v in summaries.items()
            if isinstance(v, str) and v.strip() and not is_placeholder(v)
        ]
        if cleaned_sum:
            parts.append("Summary dichiarate:\n" + "\n\n".join(cleaned_sum))

    if skills:
        parts.append(
            "Competenze attestate (uniche skill usabili per il match):\n"
            + "\n".join(f"- {s.label} [{s.category}]" for s in skills)
        )
    else:
        parts.append("Competenze attestate: nessuna (skills YAML vuote o solo placeholder).")

    exp_lines = _experience_lines(cv)
    if exp_lines:
        parts.append("Esperienze dichiarate:\n" + "\n".join(exp_lines))

    education = cv.get("education") or []
    edu_bits = []
    for item in education:
        if not isinstance(item, dict):
            continue
        degree = str(item.get("degree") or "").strip()
        inst = str(item.get("institution") or "").strip()
        if is_placeholder(degree) and is_placeholder(inst):
            continue
        edu_bits.append(" — ".join(b for b in (degree, inst) if b and not is_placeholder(b)))
    if edu_bits:
        parts.append("Formazione:\n" + "\n".join(f"- {e}" for e in edu_bits))

    if profile_md.strip():
        parts.append(
            "Additional memory (memory/profile.md; curated YAML wins on conflict):\n"
            + profile_md.strip()
        )
    return "\n\n".join(parts)


def load_profile(
    cv_path: Path = DEFAULT_CV_MASTER,
    profile_md_path: Path = DEFAULT_PROFILE_MD,
) -> Profile:
    if not cv_path.exists():
        raise SystemExit(f"Profilo YAML non trovato: {cv_path}")
    cv = yaml.safe_load(cv_path.read_text(encoding="utf-8"))
    if not isinstance(cv, dict):
        raise SystemExit(f"cv_master non valido (atteso oggetto YAML): {cv_path}")
    profile_md = ""
    if profile_md_path.exists():
        profile_md = profile_md_path.read_text(encoding="utf-8")
    skills = _collect_skill_labels(cv)
    text = build_profile_text(cv, profile_md, skills)
    return Profile(cv_master=cv, profile_md=profile_md, skills=skills, profile_text=text)


def _haystack(job: Job) -> str:
    return " ".join(
        p for p in (job.title, job.company, job.location, job.description) if p
    ).lower()


def _alias_hits(alias: str, text: str) -> bool:
    """Frasi: substring. Token singoli: word-boundary (no 'js' dentro 'javascript')."""
    if not alias:
        return False
    if " " in alias:
        return alias in text
    return bool(re.search(rf"(?<![a-z0-9_+#]){re.escape(alias)}(?![a-z0-9_+#])", text))


def lexical_overlap(job: Job, skills: Sequence[AttestedSkill]) -> tuple[float, list[str]]:
    """Saturated fraction of attested skills found in the listing text."""
    if not skills:
        return 0.0, []
    text = _haystack(job)
    title = (job.title or "").lower()
    matched: list[str] = []
    title_hits = 0
    for skill in skills:
        if any(_alias_hits(alias, text) for alias in skill.aliases):
            matched.append(skill.label)
            if any(_alias_hits(alias, title) for alias in skill.aliases):
                title_hits += 1
    n = len(matched)
    # 0 hit → 0; ~3 hit → 0.65; 6+ → ~0.88. Bonus piccolo se compaiono nel titolo.
    base = 1.0 - math.exp(-0.35 * n)
    bonus = min(0.15, 0.05 * title_hits)
    return round(min(1.0, base + bonus), 4), matched


def combine_scores(llm_score: float, lexical: float) -> float:
    """Tetto: l'LLM non puo' superare lexical + LLM_HEADROOM."""
    llm_score = max(0.0, min(1.0, llm_score))
    lexical = max(0.0, min(1.0, lexical))
    ceiling = min(1.0, lexical + LLM_HEADROOM)
    return round(min(llm_score, ceiling), 4)


def _truncate(text: str | None, limit: int = DESC_LIMIT) -> str:
    raw = (text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit].rstrip() + "\n[...troncato]"


def _normalize_score(value: Any) -> Optional[float]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    if score > 1.0:
        # il modello a volte risponde 0–100
        if score <= 100.0:
            score = score / 100.0
        else:
            score = 1.0
    return max(0.0, min(1.0, score))


def lexical_rationale(job: Job, matched: Sequence[str], skills: Sequence[AttestedSkill]) -> str:
    if not skills:
        return (
            "No attested skills in the profile (placeholders only): "
            "score lessicale nullo, nessun gonfiaggio."
        )
    missing = [s.label for s in skills if s.label not in matched]
    bits = []
    if matched:
        bits.append("Overlap attestato: " + ", ".join(matched) + ".")
    else:
        bits.append("No attested skill appears in the listing.")
    if missing:
        shown = missing[:6]
        extra = f" (+{len(missing) - 6} altre)" if len(missing) > 6 else ""
        bits.append("Non evidenziate nel testo: " + ", ".join(shown) + extra + ".")
    title = job.title or ""
    bits.append(f"Valutazione deterministica su «{title}».")
    return " ".join(bits)


def _lexical_result(job: Job, skills: Sequence[AttestedSkill]) -> MatchResult:
    score, matched = lexical_overlap(job, skills)
    return MatchResult(
        job_id=job.id,
        score=score,
        rationale=lexical_rationale(job, matched, skills),
        overlap=matched,
        gaps=[],
        lexical=score,
        method="lexical",
    )


def _jobs_payload(jobs: Sequence[Job], skills: Sequence[AttestedSkill]) -> str:
    payload = []
    for job in jobs:
        lex, matched = lexical_overlap(job, skills)
        payload.append(
            {
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "source": job.source,
                "location": job.location,
                "remote": bool(job.remote),
                "lexical_overlap": lex,
                "lexical_matched_skills": matched,
                "description": _truncate(job.description),
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def score_batch_llm(
    jobs: Sequence[Job],
    profile: Profile,
    *,
    provider: Optional[str],
) -> list[MatchResult]:
    client = get_llm_client(provider)
    log_llm_start(
        client,
        f"matching fit profilo/annunci (batch di {len(jobs)} job_id)",
    )
    user = (
        "PROFILO CANDIDATO\n"
        "=================\n"
        f"{profile.profile_text}\n\n"
        "ANNUNCI DA VALUTARE (JSON)\n"
        "==========================\n"
        f"{_jobs_payload(jobs, profile.skills)}\n\n"
        "Restituisci l'oggetto JSON dei match, un elemento per job_id."
    )
    result = client.complete(system=SYSTEM_PROMPT, user=user, max_tokens=2500)
    log_llm_usage(result)
    if not result.text:
        raise RuntimeError(
            f"Risposta vuota da {result.provider}/{result.model} "
            f"(finish_reason={result.finish_reason or 'unknown'})."
        )
    data = parse_json_object(result.text)
    items = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("JSON LLM senza lista 'matches'.")

    by_id: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            jid = int(item.get("job_id"))
        except (TypeError, ValueError):
            continue
        by_id[jid] = item

    out: list[MatchResult] = []
    for job in jobs:
        lex, matched = lexical_overlap(job, profile.skills)
        item = by_id.get(job.id)
        if not item:
            fallback = _lexical_result(job, profile.skills)
            fallback.rationale = (
                "LLM non ha restituito questo job_id; " + fallback.rationale
            )
            out.append(fallback)
            continue
        llm_score = _normalize_score(item.get("score"))
        if llm_score is None:
            fallback = _lexical_result(job, profile.skills)
            fallback.rationale = (
                "Score LLM non numerico; " + fallback.rationale
            )
            out.append(fallback)
            continue
        final = combine_scores(llm_score, lex)
        rationale = str(item.get("rationale") or "").strip()
        if not rationale:
            rationale = lexical_rationale(job, matched, profile.skills)
        if final + 1e-9 < llm_score:
            rationale += (
                f" [score tettoato a {final:.2f}: evidenza lessicale {lex:.2f}, "
                f"LLM {llm_score:.2f}]"
            )
        overlap = item.get("overlap") or matched
        if not isinstance(overlap, list):
            overlap = matched
        gaps = item.get("gaps") or []
        if not isinstance(gaps, list):
            gaps = []
        out.append(
            MatchResult(
                job_id=job.id,
                score=final,
                rationale=rationale,
                overlap=[str(x) for x in overlap if str(x).strip()],
                gaps=[str(x) for x in gaps if str(x).strip()],
                lexical=lex,
                method="llm+lexical",
            )
        )
    return out


def score_jobs(
    jobs: Sequence[Job],
    profile: Profile,
    *,
    provider: Optional[str],
    batch_size: int,
    no_llm: bool,
) -> list[MatchResult]:
    if no_llm:
        return [_lexical_result(job, profile.skills) for job in jobs]

    results: list[MatchResult] = []
    size = max(1, batch_size)
    for i in range(0, len(jobs), size):
        batch = list(jobs[i : i + size])
        print(f"Batch {i // size + 1}: job_id {[j.id for j in batch]}")
        try:
            results.extend(score_batch_llm(batch, profile, provider=provider))
        except Exception as exc:
            print(
                f"WARNING: LLM unavailable or invalid reply ({exc}). "
                "Uso solo lo score lessicale per questo batch.",
                file=sys.stderr,
            )
            results.extend(_lexical_result(job, profile.skills) for job in batch)
    return results


def fetch_pending_jobs(
    conn: sqlite3.Connection,
    *,
    force: bool,
    limit: Optional[int],
) -> list[Job]:
    if force:
        sql = """
            SELECT id, source, title, company, url, description, location, remote
            FROM jobs
            ORDER BY id
        """
        params: tuple[Any, ...] = ()
    else:
        sql = """
            SELECT j.id, j.source, j.title, j.company, j.url, j.description,
                   j.location, j.remote
            FROM jobs j
            LEFT JOIN matches m ON m.job_id = j.id
            WHERE m.job_id IS NULL
            ORDER BY j.id
        """
        params = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    return [
        Job(
            id=int(r["id"]),
            source=r["source"] or "",
            title=r["title"] or "",
            company=r["company"] or "",
            url=r["url"] or "",
            description=r["description"] or "",
            location=r["location"] or "",
            remote=int(r["remote"] or 0),
        )
        for r in rows
    ]


def upsert_profile_skills(conn: sqlite3.Connection, skills: Sequence[AttestedSkill]) -> int:
    rows = [
        (
            s.label,
            json.dumps([s.category], ensure_ascii=False),
            "cv",
            1.0,
        )
        for s in skills
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO skills (label, tags, source, confidence)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(label, source) DO UPDATE SET
            tags = excluded.tags,
            confidence = excluded.confidence
        """,
        rows,
    )
    return len(rows)


def upsert_job_skills(conn: sqlite3.Connection, results: Sequence[MatchResult]) -> None:
    rows: list[tuple[Any, ...]] = []
    for result in results:
        source = f"job:{result.job_id}"
        for label in result.gaps:
            label = label.strip()
            if not label or is_placeholder(label):
                continue
            rows.append(
                (
                    label[:120],
                    json.dumps(["job_requirement"], ensure_ascii=False),
                    source,
                    0.6,
                )
            )
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO skills (label, tags, source, confidence)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(label, source) DO UPDATE SET
            tags = excluded.tags,
            confidence = excluded.confidence
        """,
        rows,
    )


def write_matches(conn: sqlite3.Connection, results: Sequence[MatchResult]) -> None:
    computed_at = now_iso()
    conn.executemany(
        """
        INSERT INTO matches (job_id, score, rationale, computed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            score = excluded.score,
            rationale = excluded.rationale,
            computed_at = excluded.computed_at
        """,
        [(r.job_id, r.score, r.rationale, computed_at) for r in results],
    )


def fetch_top(conn: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.job_id, m.score, m.rationale, m.computed_at,
               j.title, j.company, j.source, j.url
        FROM matches m
        LEFT JOIN jobs j ON j.id = m.job_id
        ORDER BY m.score DESC, m.job_id ASC
        LIMIT ?
        """,
        (n,),
    ).fetchall()


def _ellipsize(text: str | None, width: int) -> str:
    raw = re.sub(r"\s+", " ", (text or "")).strip()
    if len(raw) <= width:
        return raw
    return raw[: width - 1].rstrip() + "…"


def print_top(conn: sqlite3.Connection, n: int) -> None:
    rows = fetch_top(conn, n)
    if not rows:
        print(
            "No matches in the table. Run `python -m matcher.matcher` "
            "to compute scores."
        )
        return
    print(f"Top {len(rows)} match (score 0.0–1.0)")
    print("-" * 88)
    for i, row in enumerate(rows, 1):
        title = row["title"] or f"(job_id {row['job_id']} no longer in jobs)"
        company = row["company"] or "?"
        source = row["source"] or "?"
        print(
            f"{i:2}. {row['score']:.2f}  {title}  — {company}  [{source}]  "
            f"(job_id={row['job_id']})"
        )
        print(f"    {_ellipsize(row['rationale'], RATIONALE_DISPLAY)}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m matcher.matcher",
        description=(
            "Profile/listing matching into ogunjob.db. "
            "With no flags, score listings not yet in matches; "
            "--top N reads the best already computed."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        default=None,
        help="Show the best N matches already computed (no rescoring unless --force).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also rescore listings already in matches (UPSERT on job_id).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Score at most N listings (useful in tests).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Listings per LLM call (default {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--provider",
        choices=list(SUPPORTED_PROVIDERS),
        default=None,
        help="LLM provider (overrides LLM_PROVIDER in .env).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the LLM: lexical overlap on attested skills only.",
    )
    parser.add_argument(
        "--cv-master",
        type=Path,
        default=DEFAULT_CV_MASTER,
        help="Path to the profile YAML (default: config/cv_master.yaml).",
    )
    parser.add_argument(
        "--profile-md",
        type=Path,
        default=DEFAULT_PROFILE_MD,
        help="Path to memory/profile.md (used if it exists).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="Path to ogunjob.db.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    if args.top is not None and args.top < 1:
        print("--top must be >= 1", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("--limit must be >= 1", file=sys.stderr)
        return 2

    conn = connect(args.db)
    try:
        consult_only = args.top is not None and not args.force
        if consult_only:
            print_top(conn, args.top)
            return 0

        profile = load_profile(args.cv_master, args.profile_md)
        n_skills = len(profile.skills)
        print(f"DB: {args.db}")
        print(f"Profile YAML: {args.cv_master}")
        if args.profile_md.exists():
            print(f"Memory: {args.profile_md}")
        else:
            print("Memory: memory/profile.md missing, using YAML only.")
        print(f"Attested skills (placeholders excluded): {n_skills}")
        for skill in profile.skills:
            print(f"  - {skill.label} [{skill.category}]")
        if n_skills == 0:
            print(
                "WARNING: no attested skills. Scores will stay low "
                "so we do not invent competencies.",
                file=sys.stderr,
            )

        upsert_profile_skills(conn, profile.skills)
        conn.commit()

        jobs = fetch_pending_jobs(conn, force=args.force, limit=args.limit)
        if not jobs:
            print(
                "No listings to score "
                + ("(--force, jobs table empty)." if args.force else
                   "(all already have a match; use --force to recompute).")
            )
            print_top(conn, args.top or DEFAULT_TOP)
            return 0

        print(f"Listings to score: {len(jobs)}" + (" [FORCE]" if args.force else ""))
        results = score_jobs(
            jobs,
            profile,
            provider=args.provider,
            batch_size=args.batch_size,
            no_llm=args.no_llm,
        )
        write_matches(conn, results)
        upsert_job_skills(conn, results)
        conn.commit()

        methods = {}
        for r in results:
            methods[r.method] = methods.get(r.method, 0) + 1
        print(f"Wrote {len(results)} matches. Methods: {methods}")
        print()
        print_top(conn, args.top or DEFAULT_TOP)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
