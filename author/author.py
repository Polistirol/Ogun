"""
author.py
---------
Generate (and, if asked, revise) a tailored CV draft + cover letter
from cv_master.yaml and a job-ad text.

Uses get_llm_client() — no direct Anthropic client.
Output is always a DRAFT to review: it must not be sent automatically.
"""

import json
from typing import List, Optional

import yaml

from llm_provider import get_llm_client, log_llm_start, log_llm_usage, parse_json_object

SYSTEM_PROMPT = """You are an assistant specialized in writing CVs and cover \
letters for software/AI/robotics roles. You receive:
1. A block-structured master CV (YAML), with tagged experience bullets.
2. The text of a job listing.

Your task:
- Choose which "headline_variant" and "summary_variant" best match the
  listing's language and priorities.
- For each experience, select and REWRITE (do not copy verbatim) the bullets
  most relevant to the listing, using the listing's keywords where possible,
  BUT without inventing facts, numbers, or technologies that are not in the
  master CV. If a fact is missing or is a placeholder like "AGGIUNGI",
  flag it clearly instead of inventing it.
- Keep a professional, direct tone, with no empty hype.
- Also write a short cover letter (max 200 words), specific to the
  company/role, that explains naturally and honestly the move from a
  (closed) startup back to employment, without a defensive tone.

Reply with a JSON object of this shape ONLY, nothing else:
{
  "headline": "...",
  "summary": "...",
  "experiences": [
    {"company": "...", "role": "...", "period": "...", "bullets": ["...", "..."]}
  ],
  "skills_highlight": ["...", "..."],
  "cover_letter": "...",
  "warnings": ["any placeholders/missing facts found in the master CV"]
}
"""


def load_cv_master(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _dump_master(cv_master: dict) -> str:
    return yaml.dump(cv_master, allow_unicode=True, sort_keys=False)


def _complete_json(user_content: str, provider: Optional[str], task: str) -> dict:
    client = get_llm_client(provider)
    log_llm_start(client, task)
    result = client.complete(system=SYSTEM_PROMPT, user=user_content, max_tokens=3000)
    log_llm_usage(result)
    text = result.text
    if not text:
        raise RuntimeError(
            f"Empty reply from {result.provider}/{result.model} "
            f"(finish_reason={result.finish_reason or 'unknown'})."
        )
    try:
        return parse_json_object(text)
    except json.JSONDecodeError:
        from pathlib import Path

        Path("raw_response.txt").write_text(text, encoding="utf-8")
        raise RuntimeError(
            "WARNING: reply is not valid JSON; raw output saved to raw_response.txt"
        )


def generate_draft(
    cv_master: dict, job_text: str, provider: Optional[str] = None
) -> dict:
    """First draft: same system prompt and JSON shape as tailor.py."""
    user_content = f"""CV_MASTER (YAML):
---
{_dump_master(cv_master)}
---

JOB LISTING:
---
{job_text}
---

Generate the JSON output as specified in the system instructions."""
    return _complete_json(
        user_content, provider, "tailored CV + cover letter generation (JSON)"
    )


def revise_draft(
    cv_master: dict,
    job_text: str,
    draft: dict,
    issues: List[str],
    provider: Optional[str] = None,
) -> dict:
    """Targeted revision: fix the flagged points, do not regenerate from scratch."""
    issues_block = "\n".join(f"- {item}" for item in issues) or "- (no detail)"
    user_content = f"""CV_MASTER (YAML):
---
{_dump_master(cv_master)}
---

JOB LISTING:
---
{job_text}
---

PREVIOUS DRAFT (JSON to correct, not to rewrite from scratch):
---
{json.dumps(draft, indent=2, ensure_ascii=False)}
---

ISSUES FLAGGED BY THE EVALUATOR — fix ONLY these points, leave the rest:
{issues_block}

Revision rules:
- Do not invent facts, numbers, or technologies absent from CV_MASTER.
- If an issue asks to cover a skill that is NOT in CV_MASTER,
  do NOT add it: remove or rephrase any such claim and flag it in warnings.
- If a fact in the master is a placeholder ("AGGIUNGI", fake dates),
  do not "resolve" it by inventing: flag it in warnings.
- Return the same JSON object, updated.

Generate the JSON output as specified in the system instructions."""
    return _complete_json(
        user_content,
        provider,
        "targeted revision of tailored CV + cover letter (JSON)",
    )


def render_markdown_cv(data: dict, candidate: dict) -> str:
    lines = [f"# {candidate.get('name', '')}", ""]
    lines.append(f"**{data['headline']}**")
    lines.append("")
    lines.append(
        f"{candidate.get('location', '')} | "
        f"{candidate.get('links', {}).get('email', '')} | "
        f"{candidate.get('links', {}).get('linkedin', '')} | "
        f"{candidate.get('links', {}).get('github', '')}"
    )
    lines.append("")
    lines.append("## Profile")
    lines.append(data["summary"])
    lines.append("")
    lines.append("## Experience")
    for exp in data["experiences"]:
        lines.append(f"### {exp['role']} — {exp['company']} ({exp['period']})")
        for b in exp["bullets"]:
            lines.append(f"- {b}")
        lines.append("")
    lines.append("## Skills")
    lines.append(", ".join(data.get("skills_highlight", [])))
    return "\n".join(lines)
