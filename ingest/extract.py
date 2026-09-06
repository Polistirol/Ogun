"""
ingest/extract.py
------------------
Call the model to extract, from CV + GitHub repos + extra documents,
a structured "raw" profile to review by hand before it flows into the
definitive cv_master.yaml used by tailor.py.

KEY RULE: extraction summarizes what is in the sources; it does not invent.
Every generated block carries needs_review + review_note.

Writing to memory/profile.md does NOT happen automatically here:
use `commit_validated_to_memory` / `build_profile.py --commit-to-memory`
after human review.
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from llm_provider import get_llm_client, log_llm_start, log_llm_usage, parse_json_object

SYSTEM_PROMPT = """You are an assistant that builds a structured professional \
profile from provided sources: CV text, a list of GitHub repositories \
(with description and README), and optional extra documents \
(papers, project notes).

FUNDAMENTAL RULE: invent nothing. Extract and rephrase only what is \
explicitly present in the provided sources. If something is ambiguous, \
incomplete, or seems missing (dates, metrics, unspecified results), \
do NOT fill it in on your own: flag the block with needs_review=true \
and explain what is missing in review_note.

For GitHub repositories, use description and README to understand what \
the project is and which area it belongs to (tags: ai_agents, robotics, \
backend, or another if more relevant), but do not attribute skills that \
the real repo content does not suggest.

Reply with a JSON object of this shape ONLY, nothing else:
{
  "experiences": [
    {
      "source": "cv",
      "company": "...",
      "role": "...",
      "period": "...",
      "raw_bullets": ["...", "..."],
      "needs_review": true,
      "review_note": "..."
    }
  ],
  "projects": [
    {
      "source": "github:repo-name",
      "title": "...",
      "summary": "...",
      "tags": ["ai_agents"],
      "tech_stack": ["..."],
      "needs_review": false,
      "review_note": ""
    }
  ],
  "skills_candidates": ["..."],
  "gaps": ["general notes on what is missing or unclear in the overall profile"]
}
"""


def _format_db_skill_line(skill: Dict) -> str:
    bits = [skill.get("label") or ""]
    extra = []
    if skill.get("source"):
        extra.append(f"source={skill['source']}")
    tags = skill.get("tags") or []
    if tags:
        extra.append("tags=" + ",".join(str(t) for t in tags))
    conf = skill.get("confidence")
    if conf is not None:
        extra.append(f"confidence={conf}")
    if extra:
        bits.append(f"({'; '.join(extra)})")
    return "- " + " ".join(bits)


def build_user_content(
    cv_text: str,
    repos: List[Dict],
    extra_docs: List[Dict],
    db_skills: Optional[List[Dict]] = None,
) -> str:
    parts = [f"=== CV ===\n{cv_text}\n"]

    if repos:
        parts.append("=== REPOSITORY GITHUB ===")
        for r in repos:
            parts.append(
                f"- {r['name']} ({r['language']}, {r['stars']} stars)\n"
                f"  description: {r['description']}\n"
                f"  URL: {r['url']}\n"
                f"  README (excerpt): {r['readme_excerpt'][:1500]}\n"
            )

    if extra_docs:
        parts.append("=== EXTRA DOCUMENTS ===")
        for d in extra_docs:
            parts.append(f"--- {d['name']} ---\n{d['text']}\n")

    if db_skills:
        parts.append(
            "=== ALREADY CATALOGUED SKILLS (ogunjob.db, optional) ===\n"
            "These skills were already extracted by other tasks. Use them only "
            "as context: do NOT invent skills absent from the primary sources "
            "(CV, GitHub, documents). If a DB skill is not supported by those "
            "sources, ignore it.\n"
        )
        for s in db_skills:
            parts.append(_format_db_skill_line(s))
        parts.append("")

    return "\n".join(parts)


def extract_profile(
    cv_text: str,
    repos: List[Dict],
    extra_docs: List[Dict],
    dry_run: bool = False,
    provider: Optional[str] = None,
    db_skills: Optional[List[Dict]] = None,
) -> Dict:
    user_content = build_user_content(cv_text, repos, extra_docs, db_skills=db_skills)

    if dry_run:
        print("=== DRY RUN: no API call, this is what would be sent ===\n")
        print("--- SYSTEM PROMPT (excerpt) ---")
        print(SYSTEM_PROMPT[:400] + "...\n")
        print(f"--- USER CONTENT ({len(user_content)} characters) ---")
        preview_limit = 8000
        print(user_content[:preview_limit])
        if len(user_content) > preview_limit:
            print(f"\n... [truncated: {len(user_content) - preview_limit} more characters]")
        return {}

    client = get_llm_client(provider)
    log_llm_start(
        client,
        "structured profile extraction (JSON) from CV + GitHub + extra documents",
    )
    result = client.complete(system=SYSTEM_PROMPT, user=user_content, max_tokens=4000)
    log_llm_usage(result)
    text = result.text
    if not text:
        raise RuntimeError(
            f"Empty reply from {result.provider}/{result.model} "
            f"(finish_reason={result.finish_reason or 'unknown'}). "
            "With DeepSeek V4, default thinking can exhaust tokens without "
            "producing JSON: this tool disables it. To turn it back on, set "
            "DEEPSEEK_THINKING=1 in .env (and raise max_tokens)."
        )

    try:
        return parse_json_object(text)
    except json.JSONDecodeError:
        Path("raw_extract_response.txt").write_text(text, encoding="utf-8")
        raise RuntimeError(
            "Model reply is not valid JSON: saved to raw_extract_response.txt for debug."
        )


# ---------------------------------------------------------------------------
# Commit to memory/profile.md — ONLY on explicit invocation.
# The normal flow writes the YAML draft only; this step happens after
# human review (blocks with needs_review: false).
# ---------------------------------------------------------------------------

_DEFAULT_MEMORY_PATH = Path(__file__).resolve().parent.parent / "memory" / "profile.md"


def split_validated(data: Dict) -> Tuple[Dict, int, int]:
    """Split already-reviewed blocks (needs_review=false) from still-open ones."""
    experiences = data.get("experiences") or []
    projects = data.get("projects") or []
    kept_exp = [e for e in experiences if isinstance(e, dict) and not e.get("needs_review", True)]
    kept_proj = [p for p in projects if isinstance(p, dict) and not p.get("needs_review", True)]
    skipped = (len(experiences) - len(kept_exp)) + (len(projects) - len(kept_proj))
    kept = len(kept_exp) + len(kept_proj)
    validated = {
        "experiences": kept_exp,
        "projects": kept_proj,
        "skills_candidates": list(data.get("skills_candidates") or []),
        "gaps": list(data.get("gaps") or []),
    }
    return validated, kept, skipped


def _md_section_map(text: str) -> Dict[str, str]:
    """Map H2 heading ('## ...') -> full block, to preserve existing sections."""
    if not text:
        return {}
    parts = re.split(r"(?m)^(## [^#].+)$", text)
    sections: Dict[str, str] = {}
    i = 1
    while i + 1 < len(parts):
        heading = parts[i][3:].strip()
        body = parts[i + 1]
        sections[heading] = (parts[i] + body).rstrip() + "\n"
        i += 2
    return sections


def _bullet_list(items: List[str]) -> str:
    return "\n".join(f"- {item}" for item in items if str(item).strip())


def render_memory_markdown(
    validated: Dict,
    *,
    existing_md: str = "",
    source_label: str = "config/master_profile_draft.yaml",
    skipped: int = 0,
) -> str:
    """Render memory/profile.md from validated blocks only, keeping identity if already present."""
    existing = _md_section_map(existing_md)
    today = date.today().isoformat()

    def _existing(*headings: str) -> str:
        for heading in headings:
            if existing.get(heading):
                return existing[heading].rstrip()
        return ""

    lines = [
        "# Profile",
        "",
        f"- **last_updated:** {today}",
        f"- **updated_by:** build_profile.py --commit-to-memory (validated blocks from `{source_label}`)",
        "- **who updates it:** rarely, after human review of an extraction draft. "
        "The master does not overwrite this from conversation. "
        "`config/cv_master.yaml` remains the curated source used by tailor/Author.",
        "",
        "---",
        "",
        "Updated from draft blocks with `needs_review: false`. "
        "Blocks still in review were not copied.",
    ]
    if skipped:
        lines.append(
            f"Skipped {skipped} blocks with `needs_review: true` — they stay in the YAML draft."
        )
    lines.append("")

    identity = _existing("Identity", "Anagrafica")
    if identity:
        lines.append(identity)
    else:
        lines.extend(
            [
                "## Identity",
                "",
                "_Not present in the extraction draft. Keep it in `config/cv_master.yaml` "
                "or in the previous seed of `memory/profile.md`._",
            ]
        )
    lines.append("")

    for heading in (
        ("Headline (variants)", "Headline (varianti)"),
        ("Summary (variants)", "Summary (varianti)"),
    ):
        kept = _existing(*heading)
        if kept:
            lines.append(kept)
            lines.append("")

    lines.append("## Experience")
    lines.append("")
    experiences = validated.get("experiences") or []
    if not experiences:
        lines.append("_No validated experience in the draft._")
        lines.append("")
    for exp in experiences:
        company = exp.get("company") or "Unspecified company"
        role = exp.get("role") or ""
        title = f"{company} — {role}".rstrip(" —")
        lines.append(f"### {title}")
        lines.append("")
        if exp.get("period"):
            lines.append(f"- **Period:** {exp['period']}")
        if exp.get("source"):
            lines.append(f"- **Source:** {exp['source']}")
        lines.append("")
        for bullet in exp.get("raw_bullets") or []:
            lines.append(f"- {bullet}")
        lines.append("")

    lines.append("## Projects")
    lines.append("")
    projects = validated.get("projects") or []
    if not projects:
        lines.append("_No validated projects in the draft._")
        lines.append("")
    for proj in projects:
        title = proj.get("title") or proj.get("source") or "Project"
        lines.append(f"### {title}")
        lines.append("")
        if proj.get("source"):
            lines.append(f"- **Source:** {proj['source']}")
        tags = proj.get("tags") or []
        if tags:
            lines.append(f"- **Tags:** {', '.join(str(t) for t in tags)}")
        stack = proj.get("tech_stack") or []
        if stack:
            lines.append(f"- **Stack:** {', '.join(str(t) for t in stack)}")
        if proj.get("summary"):
            lines.append("")
            lines.append(str(proj["summary"]).strip())
        lines.append("")

    lines.append("## Skills")
    lines.append("")
    skills = validated.get("skills_candidates") or []
    if skills:
        lines.append("Candidate skills from the validated draft (they do not replace `config/cv_master.yaml`):")
        lines.append("")
        lines.append(_bullet_list([str(s) for s in skills]))
    else:
        lines.append("_No candidate skills in the draft._")
    lines.append("")

    education = _existing("Education", "Formazione")
    if education:
        lines.append(education)
        lines.append("")
    else:
        lines.extend(
            [
                "## Education",
                "",
                "_Not present in the extraction draft._",
                "",
            ]
        )

    gaps = validated.get("gaps") or []
    if gaps:
        lines.append("## Open gaps")
        lines.append("")
        lines.append(_bullet_list([str(g) for g in gaps]))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def commit_validated_to_memory(
    data: Dict,
    dest: Optional[Path] = None,
    *,
    source_label: str = "config/master_profile_draft.yaml",
    dry_run: bool = False,
) -> Tuple[str, int, int]:
    """
    Write validated blocks to memory/profile.md.

    Never called from the extraction flow: only from
    `build_profile.py --commit-to-memory`. Returns
    (markdown, n_blocks_written, n_blocks_skipped).
    """
    dest = Path(dest) if dest is not None else _DEFAULT_MEMORY_PATH
    validated, kept, skipped = split_validated(data)
    skills_n = len(validated.get("skills_candidates") or [])
    if kept == 0 and skills_n == 0:
        raise RuntimeError(
            "No validated block to copy into memory/: set "
            "`needs_review: false` on reviewed blocks in the YAML draft "
            "before --commit-to-memory."
        )

    existing_md = ""
    if dest.exists():
        existing_md = dest.read_text(encoding="utf-8")

    markdown = render_memory_markdown(
        validated,
        existing_md=existing_md,
        source_label=source_label,
        skipped=skipped,
    )
    if dry_run:
        return markdown, kept, skipped

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(markdown, encoding="utf-8")
    return markdown, kept, skipped
