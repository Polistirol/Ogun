"""
evaluator.py
------------
Score an Author draft (CV + cover letter) against the job ad and
cv_master.yaml. Does not rewrite: it flags concrete problems.

RULE: never invent facts. A job-ad keyword missing from the master is
NOT a draft defect if omitted; it is a defect if the draft covers it
by inventing.
"""

import json
from typing import Optional

import yaml

from llm_provider import get_llm_client, log_llm_start, log_llm_usage, parse_json_object

PASS_THRESHOLD = 75

SYSTEM_PROMPT = """You are a critical evaluator of CV and cover-letter drafts.
You are not a writer: you do not rewrite the text, you judge it.

You receive three inputs: the job listing, the CV_MASTER (YAML, the only \
factual source of truth), and the generated DRAFT (JSON: headline, summary, \
experiences, skills_highlight, cover_letter, warnings).

FUNDAMENTAL RULE: never invent facts. Score ONLY against CV_MASTER \
and the listing. If a keyword/skill required by the listing is NOT \
in CV_MASTER, it is NOT a draft defect if omitted: it is correct \
not to invent it. It is a defect if the draft pretends to cover it by \
inventing technologies, numbers, roles, or results.

Criteria (concrete, not aesthetic):
1. Coverage — of the listing keywords/skills that are ACTUALLY present \
in CV_MASTER (not placeholders), how many are recognizably reflected \
in the draft (headline, summary, bullets, skills_highlight, cover letter)? \
Penalize omissions of real skills, not omissions of skills absent from the master.
2. Factual consistency — no claim unsupported by CV_MASTER. \
Every invented fact (technology, number, date, seniority like "Senior" \
if the master only says "Software Engineer") is a serious issue.
3. Clarity and concision — professional tone, no empty hype, \
short cover letter (about 200 words max), specific bullets.

NOT a defect (do not lower the score, do not put it in issues):
- CV_MASTER placeholders left explicit or flagged in warnings \
("AGGIUNGI", "Startup Name", dates to update, "SPECIFICA"): it is correct \
not to invent.
- Listing keywords absent from the master and therefore omitted (e.g. Kubernetes \
if it is not in the YAML).
- Structural fields not in the draft JSON (education, contacts): \
the renderer adds those, not the Author.

Indicative weight on the 0-100 score: coverage 40, factual consistency 40, \
clarity 20. A serious factual violation must drop the score a lot \
even if the rest is good.

issues: SHORT list of SPECIFIC, actionable problems (cite the offending \
piece). At most 5 issues, only real defects. If there are none, \
empty list.

passed: true if and only if score >= 75.

Reply with a JSON object ONLY, nothing else:
{
  "score": 0,
  "issues": ["..."],
  "passed": false
}
"""


def _normalize(raw: dict) -> dict:
    try:
        score = int(raw.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    issues = raw.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    issues = [str(item).strip() for item in issues if str(item).strip()]
    return {
        "score": score,
        "issues": issues,
        "passed": score >= PASS_THRESHOLD,
    }


def evaluate(
    cv_master: dict,
    job_text: str,
    draft: dict,
    provider: Optional[str] = None,
) -> dict:
    """
    Restituisce {"score": int, "issues": [str], "passed": bool}.
    `passed` is derived from score >= PASS_THRESHOLD, not from the model's bool.
    """
    user_content = f"""CV_MASTER (YAML) — only factual source of truth:
---
{yaml.dump(cv_master, allow_unicode=True, sort_keys=False)}
---

ANNUNCIO DI LAVORO:
---
{job_text}
---

BOZZA DELL'AUTHOR (JSON):
---
{json.dumps(draft, indent=2, ensure_ascii=False)}
---

Score the draft and return JSON as specified in the system instructions."""

    client = get_llm_client(provider)
    log_llm_start(client, "CV/cover letter draft evaluation (JSON)")
    result = client.complete(system=SYSTEM_PROMPT, user=user_content, max_tokens=1500)
    log_llm_usage(result)
    text = result.text
    if not text:
        raise RuntimeError(
            f"Risposta vuota da {result.provider}/{result.model} "
            f"(finish_reason={result.finish_reason or 'sconosciuto'})."
        )
    try:
        raw = parse_json_object(text)
    except json.JSONDecodeError:
        from pathlib import Path

        Path("raw_evaluator_response.txt").write_text(text, encoding="utf-8")
        raise RuntimeError(
            "ATTENZIONE: valutazione non in JSON valido, "
            "output grezzo salvato in raw_evaluator_response.txt"
        )
    return _normalize(raw)
