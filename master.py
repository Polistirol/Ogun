#!/usr/bin/env python3
"""
master.py
---------
Conversational entrypoint for OgunJob.

REPL loop: read input, reply via llm_provider, update markdown memory in
memory/. Does not yet orchestrate scraper / extraction / author / matcher:
AVAILABLE_TOOLS is the hook for that next step.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from llm_provider import (
    LLMClient,
    SUPPORTED_PROVIDERS,
    get_llm_client,
    parse_json_object,
)

def _configure_stdio() -> None:
    """On Windows the console is often cp1252: prefer UTF-8 with replace."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


ROOT = Path(__file__).resolve().parent
DEFAULT_MEMORY_DIR = ROOT / "memory"

MEMORY_FILES = (
    "profile.md",
    "search-criteria.md",
    "market-insights.md",
    "decisions-log.md",
)

CRITERIA_PLACEHOLDER = "_No criteria set._"
CRITERIA_HISTORY_PLACEHOLDER = "_No updates._"
DECISIONS_PLACEHOLDER = "_No decisions recorded._"

# Canonical English headings, plus Italian aliases for existing local files.
CRITERIA_SECTIONS = {
    "roles": "## Target roles",
    "location": "## Location and work mode",
    "exclusions": "## Exclusions",
    "companies": "## Companies",
    "salary": "## Salary range",
    "sources": "## Preferred sources / providers",
    "other": "## Other criteria",
}

CRITERIA_SECTION_ALIASES = {
    "roles": ("## Ruoli target",),
    "location": ("## Località e modalità",),
    "exclusions": ("## Esclusioni",),
    "companies": ("## Aziende",),
    "salary": ("## Range salariale",),
    "sources": ("## Fonti / provider preferiti",),
    "other": ("## Altri criteri",),
}

CRITERIA_SECTION_HEADING = "## Update history"
CRITERIA_SECTION_HEADING_ALIASES = ("## Cronologia aggiornamenti",)

DECISIONS_HEADING = "## Entries"
DECISIONS_HEADING_ALIASES = ("## Voci",)

EMPTY_CRITERIA = (
    CRITERIA_PLACEHOLDER,
    "_Nessun criterio impostato._",
)
EMPTY_HISTORY = (
    CRITERIA_HISTORY_PLACEHOLDER,
    "_Nessun aggiornamento._",
)
EMPTY_DECISIONS = (
    DECISIONS_PLACEHOLDER,
    "_Nessuna decisione registrata._",
)
UPDATED_PREFIXES = ("_Updated:", "_Aggiornato:")

# ---------------------------------------------------------------------------
# Extension point: tools from other components (not wired yet)
# ---------------------------------------------------------------------------

AVAILABLE_TOOLS: Dict[str, Dict[str, Any]] = {
    "scrape_jobs": {
        "description": "Run the scraper swarm on public sources.",
        "handler": None,
    },
    "extract_profile": {
        "description": "Extract a profile draft from CV/GitHub.",
        "handler": None,
    },
    "author_cv": {
        "description": "Generate a tailored CV and cover letter, with evaluator.",
        "handler": None,
    },
    "match_jobs": {
        "description": "Score profile/listing fit.",
        "handler": None,
    },
}


def dispatch_tool(name: str, **kwargs: Any) -> Any:
    """Call a registered tool. No handler is wired yet."""
    spec = AVAILABLE_TOOLS.get(name)
    if spec is None:
        raise KeyError(
            f"Unknown tool: {name!r}. "
            f"Available: {', '.join(AVAILABLE_TOOLS)}"
        )
    handler: Optional[Callable[..., Any]] = spec.get("handler")
    if handler is None:
        raise NotImplementedError(
            f"Tool {name!r} is in the registry but not wired yet. "
            "It will be hooked up when scraper/extraction/author/matcher are ready."
        )
    return handler(**kwargs)


def _heading_candidates(canonical: str, aliases: Sequence[str] = ()) -> Tuple[str, ...]:
    return (canonical, *aliases)


def _find_heading(text: str, canonical: str, aliases: Sequence[str] = ()) -> str:
    for heading in _heading_candidates(canonical, aliases):
        if _section_body(text, heading) is not None:
            return heading
    return canonical


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def memory_dir() -> Path:
    override = os.environ.get("OGUNJOB_MEMORY_DIR")
    if override:
        return Path(override)
    return DEFAULT_MEMORY_DIR


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _read_utf8(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_utf8(path: Path, text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def load_memory(mem_dir: Optional[Path] = None) -> Dict[str, str]:
    """Read every markdown file in memory/ (missing file → empty string)."""
    root = mem_dir or memory_dir()
    loaded: Dict[str, str] = {}
    for name in MEMORY_FILES:
        path = root / name
        if path.is_file():
            loaded[name] = _read_utf8(path)
        else:
            loaded[name] = ""
    return loaded


def _set_header_field(text: str, field: str, value: str) -> str:
    pattern = rf"(^- \*\*{re.escape(field)}:\*\* ).+$"
    repl = rf"\g<1>{value}"
    new, n = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    return new if n else text


def _touch_header(text: str, when: str, updated_by: str) -> str:
    text = _set_header_field(text, "last_updated", when)
    text = _set_header_field(text, "updated_by", updated_by)
    return text


def _section_body(text: str, heading: str) -> Optional[str]:
    pattern = re.compile(
        rf"^{re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1)


def _replace_section(text: str, heading: str, new_body: str) -> str:
    body = new_body.rstrip() + "\n\n"
    pattern = re.compile(
        rf"(^{re.escape(heading)}\s*\n)(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    replacement = rf"\g<1>{body}"
    new, n = re.subn(pattern, replacement, text, count=1)
    if n:
        return new
    return text.rstrip() + f"\n\n{heading}\n\n{body}"


def _normalize_bullet(statement: str) -> str:
    line = " ".join(statement.strip().split())
    if line.startswith("- "):
        line = line[2:].strip()
    return line


def apply_search_criteria(
    statement: str,
    section: str,
    mode: str = "append",
    mem_dir: Optional[Path] = None,
    when: Optional[str] = None,
) -> bool:
    """
    Update the relevant section of search-criteria.md and append a
    history line. `mode=replace` rewrites the section; `mode=append`
    adds a bullet (or replaces the placeholder).
    Returns False if there was nothing new to write.
    """
    root = mem_dir or memory_dir()
    path = root / "search-criteria.md"
    text = _read_utf8(path) if path.is_file() else ""
    when = when or now_iso()
    key = section if section in CRITERIA_SECTIONS else "other"
    heading = _find_heading(
        text, CRITERIA_SECTIONS[key], CRITERIA_SECTION_ALIASES.get(key, ())
    )
    bullet = _normalize_bullet(statement)
    if not bullet:
        return False

    current = _section_body(text, heading)
    if current is None:
        current = CRITERIA_PLACEHOLDER + "\n"

    stamp = f"_Updated: {when}_"
    existing_lines = [
        ln.rstrip()
        for ln in current.strip().splitlines()
        if ln.strip()
        and ln.strip() not in EMPTY_CRITERIA
        and not ln.strip().startswith(UPDATED_PREFIXES)
    ]
    existing_bullets = [ln[2:].strip() if ln.startswith("- ") else ln for ln in existing_lines]

    if mode != "replace" and bullet in existing_bullets:
        return False

    if mode == "replace":
        new_body = f"{stamp}\n\n- {bullet}\n"
    elif not existing_lines:
        new_body = f"{stamp}\n\n- {bullet}\n"
    else:
        kept = "\n".join(
            ln if ln.startswith("- ") else f"- {ln}" for ln in existing_lines
        )
        new_body = f"{stamp}\n\n{kept}\n- {bullet}\n"

    text = _replace_section(text, heading, new_body)

    history_heading = _find_heading(
        text, CRITERIA_SECTION_HEADING, CRITERIA_SECTION_HEADING_ALIASES
    )
    history = _section_body(text, history_heading)
    hist_line = f"- `{when}` · {heading[3:]} · {bullet}"
    if history is None or any(p in (history or "") for p in EMPTY_HISTORY):
        hist_body = hist_line + "\n"
    else:
        hist_body = history.rstrip() + "\n" + hist_line + "\n"
    text = _replace_section(text, history_heading, hist_body)
    text = _touch_header(text, when, "master agent (conversation)")
    _write_utf8(path, text)
    return True


def apply_decision(
    statement: str,
    mem_dir: Optional[Path] = None,
    when: Optional[str] = None,
) -> bool:
    """Append-only on decisions-log.md. Does not rewrite previous entries."""
    root = mem_dir or memory_dir()
    path = root / "decisions-log.md"
    text = _read_utf8(path) if path.is_file() else ""
    when = when or now_iso()
    entry = _normalize_bullet(statement)
    if not entry:
        return False

    heading = _find_heading(text, DECISIONS_HEADING, DECISIONS_HEADING_ALIASES)
    current = _section_body(text, heading)
    if current and not any(p in current for p in EMPTY_DECISIONS) and entry in current:
        return False

    block = f"### {when}\n\n{entry}\n"
    if current is None or any(p in (current or "") for p in EMPTY_DECISIONS):
        new_body = block
    else:
        new_body = current.rstrip() + "\n\n" + block
    text = _replace_section(text, heading, new_body)
    text = _touch_header(text, when, "master agent (conversation)")
    _write_utf8(path, text)
    return True


# ---------------------------------------------------------------------------
# Classification: search criterion vs decision to log
# ---------------------------------------------------------------------------

_DECISION_RE = re.compile(
    r"\b("
    r"ho deciso|ho scelto|decido di|scelgo di|"
    r"apro la p\.?\s*iva|aprire la p\.?\s*iva|"
    r"ho aperto la p\.?\s*iva"
    r")\b",
    re.IGNORECASE,
)

_CRITERIA_RE = re.compile(
    r"\b("
    r"cerco|escludi|escludo|escludiamo|"
    r"solo ruoli|solo lavori|solo posizioni|"
    r"full[\s-]?remote|remote|ibrido|on[\s-]?site|"
    r"filtro|criteri[oi]|"
    r"non voglio|non considerare|niente ruoli|"
    r"ral\b|stipendio|salario|fascia salarial|"
    r"aziende? target|solo aziende"
    r")\b",
    re.IGNORECASE,
)

_QUESTION_RE = re.compile(
    r"\b(dovrei|secondo te|mi conviene|che ne pensi|quali sono|cosa ne pensi)\b",
    re.IGNORECASE,
)

_LOCATION_RE = re.compile(
    r"\b(remote|remoto|remoti|eu\b|europe|usa\b|ibrido|on[\s-]?site|"
    r"in sede|full[\s-]?remote|timezone|fuso)\b",
    re.IGNORECASE,
)
_EXCLUSION_RE = re.compile(
    r"\b(escludi|escludo|escludiamo|niente|non voglio|non considerare|no più)\b",
    re.IGNORECASE,
)
_SALARY_RE = re.compile(
    r"\b(ral\b|stipendio|salario|fascia salarial|compenso|k€|€)\b",
    re.IGNORECASE,
)
_COMPANY_RE = re.compile(
    r"\b(aziend[ae]|company|companies|startup only|no startup)\b",
    re.IGNORECASE,
)
_SOURCE_RE = re.compile(
    r"\b(weworkremotely|remoteok|linkedin|board|bacheca|fonte|rss)\b",
    re.IGNORECASE,
)
_ROLE_RE = re.compile(
    r"\b(ruol[oi]|lavor[oi]|posizion[ei]|backend|frontend|fullstack|"
    r"engineer|sviluppatore)\b",
    re.IGNORECASE,
)


@dataclass
class CriteriaUpdate:
    section: str
    statement: str
    mode: str = "append"


@dataclass
class DecisionUpdate:
    statement: str


@dataclass
class TurnClassification:
    criteria: Optional[CriteriaUpdate] = None
    decision: Optional[DecisionUpdate] = None


def _looks_like_question(text: str) -> bool:
    stripped = text.strip()
    if stripped.endswith("?"):
        return True
    return bool(_QUESTION_RE.search(stripped))


def _guess_criteria_section(text: str) -> str:
    if _EXCLUSION_RE.search(text):
        return "exclusions"
    if _SALARY_RE.search(text):
        return "salary"
    if _SOURCE_RE.search(text):
        return "sources"
    if _LOCATION_RE.search(text):
        return "location"
    if _COMPANY_RE.search(text):
        return "companies"
    if _ROLE_RE.search(text):
        return "roles"
    return "other"


def classify_heuristic(text: str) -> TurnClassification:
    """
    Conservative deterministic fallback. Covers explicit cases
    ("I only want remote EU roles", "exclude robotics",
    "I decided to open a VAT number") without inferring extra content.
    """
    raw = text.strip()
    if not raw:
        return TurnClassification()

    is_question = _looks_like_question(raw)
    decision = None
    if _DECISION_RE.search(raw) and not is_question:
        decision = DecisionUpdate(statement=raw)

    criteria = None
    # A criteria command still counts if phrased as a weak question
    # ("exclude robotics?"), but not requests for advice.
    advice = bool(_QUESTION_RE.search(raw))
    if _CRITERIA_RE.search(raw) and not advice:
        criteria = CriteriaUpdate(
            section=_guess_criteria_section(raw),
            statement=raw,
            mode="append",
        )

    return TurnClassification(criteria=criteria, decision=decision)


CLASSIFY_SYSTEM = """\
You are a classifier for the OgunJob master agent.
You analyze ONE user message and decide whether it contains:
1) a new job-search criterion to save in search-criteria.md
2) a decision already taken to append in decisions-log.md

RULES:
- Invent nothing. The "statement" must use only what the user said.
- If the user asks for advice ("should I open a VAT number?", "what do you think...")
  it is NOT a decision.
- If the user asks what the current criteria are, it is NOT a new criterion.
- "I only want remote EU roles", "exclude robotics" → search_criteria.
- "I decided to open a VAT number" → decision.
- One sentence can be BOTH (e.g. "I decided to exclude robotics").
- If it is only chat, a greeting, or a generic question: both null.

Allowed section values: roles, location, exclusions, companies, salary, sources, other
mode: append (add to the set) or replace (rewrite the section)

Reply with a JSON object ONLY, nothing else:
{
  "search_criteria": null,
  "decision": null
}

Criterion example:
{
  "search_criteria": {
    "section": "location",
    "statement": "I only want remote EU roles",
    "mode": "replace"
  },
  "decision": null
}

Decision example:
{
  "search_criteria": null,
  "decision": {
    "statement": "I decided to open a VAT number"
  }
}
"""


def _parse_classification_json(data: dict) -> TurnClassification:
    criteria = None
    raw_c = data.get("search_criteria")
    if isinstance(raw_c, dict):
        statement = str(raw_c.get("statement") or "").strip()
        section = str(raw_c.get("section") or "other").strip().lower()
        mode = str(raw_c.get("mode") or "append").strip().lower()
        if section not in CRITERIA_SECTIONS:
            section = "other"
        if mode not in ("append", "replace"):
            mode = "append"
        if statement:
            criteria = CriteriaUpdate(section=section, statement=statement, mode=mode)

    decision = None
    raw_d = data.get("decision")
    if isinstance(raw_d, dict):
        statement = str(raw_d.get("statement") or "").strip()
        if statement:
            decision = DecisionUpdate(statement=statement)
    elif isinstance(raw_d, str) and raw_d.strip():
        decision = DecisionUpdate(statement=raw_d.strip())

    return TurnClassification(criteria=criteria, decision=decision)


def classify_with_llm(client: LLMClient, text: str) -> TurnClassification:
    result = client.complete(
        system=CLASSIFY_SYSTEM,
        user=text,
        max_tokens=400,
    )
    data = parse_json_object(result.text)
    if not isinstance(data, dict):
        raise ValueError("classification: JSON is not an object")
    return _parse_classification_json(data)


def classify_user_message(
    text: str,
    client: Optional[LLMClient] = None,
) -> TurnClassification:
    """Dedicated LLM (JSON) if available, otherwise heuristics."""
    if client is not None:
        try:
            return classify_with_llm(client, text)
        except Exception as exc:
            print(
                f"(LLM classification failed, using heuristics: {exc})",
                file=sys.stderr,
            )
    return classify_heuristic(text)


def apply_classification(
    classified: TurnClassification,
    mem_dir: Optional[Path] = None,
) -> List[str]:
    """Apply memory updates. Returns labels of what was written."""
    notes: List[str] = []
    when = now_iso()
    if classified.criteria is not None:
        wrote = apply_search_criteria(
            classified.criteria.statement,
            classified.criteria.section,
            classified.criteria.mode,
            mem_dir=mem_dir,
            when=when,
        )
        if wrote:
            notes.append(
                f"criterion -> search-criteria.md [{classified.criteria.section}]"
            )
    if classified.decision is not None:
        wrote = apply_decision(
            classified.decision.statement, mem_dir=mem_dir, when=when
        )
        if wrote:
            notes.append("decision -> decisions-log.md")
    return notes


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


def build_system_prompt(memory: Dict[str, str]) -> str:
    tools_lines = []
    for name, spec in AVAILABLE_TOOLS.items():
        status = "wired" if spec.get("handler") else "not wired"
        tools_lines.append(f"- {name} ({status}): {spec['description']}")
    tools_block = "\n".join(tools_lines)

    chunks = [
        "You are the OgunJob master agent, a local personal tool for job "
        "search (tailored CV, listing matching, tracking). "
        "Reply in the user's language, directly and concretely.",
        "Do not invent facts about experience, skills, compensation, or "
        "identity that are not already in the memory files or that the user "
        "just stated. If a fact is missing, say so.",
        "Human review is mandatory before using an extracted profile or a "
        "generated CV: do not promise automatic submissions.",
        "In this turn you CANNOT run scraper, extraction, author, or "
        "matcher. If the user asks, explain that it comes in a later step. "
        "Tool registry (reference only, do not call them):\n"
        + tools_block,
        "If you updated memory in this turn, confirm it in one line. "
        "Do not repeat the whole file.",
        "## Persistent memory (current file contents)",
    ]
    for name in MEMORY_FILES:
        body = (memory.get(name) or "").strip() or "(empty or missing file)"
        chunks.append(f"### {name}\n{body}")
    return "\n\n".join(chunks)


def _format_history(history: List[Tuple[str, str]], current: str) -> str:
    parts: List[str] = []
    if history:
        parts.append("## Recent history")
        for role, content in history[-12:]:
            label = "User" if role == "user" else "Assistant"
            parts.append(f"{label}: {content}")
        parts.append("")
    parts.append("## Current message")
    parts.append(current)
    return "\n".join(parts)


def _extract_reply_text(client: LLMClient, raw: str) -> str:
    """DeepSeek forces json_object: accept {\"reply\": \"...\"} without changing the provider."""
    text = (raw or "").strip()
    if client.provider != "deepseek":
        return text
    try:
        data = parse_json_object(text)
    except Exception:
        return text
    if isinstance(data, dict):
        for key in ("reply", "text", "message", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return text


def conversational_reply(
    client: LLMClient,
    memory: Dict[str, str],
    history: List[Tuple[str, str]],
    user_text: str,
    memory_notes: List[str],
) -> str:
    system = build_system_prompt(memory)
    if client.provider == "deepseek":
        system += (
            "\n\nReply ONLY with a JSON object of the form "
            '{"reply": "text to show the user"}.'
        )
    extra = ""
    if memory_notes:
        extra = (
            "\n\n[System: memory was updated in this turn: "
            + "; ".join(memory_notes)
            + ". Confirm it briefly.]"
        )
    user_payload = _format_history(history, user_text) + extra
    result = client.complete(system=system, user=user_payload, max_tokens=1500)
    return _extract_reply_text(client, result.text)


# ---------------------------------------------------------------------------
# One turn (REPL and frontend)
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    """Outcome of one turn: used by both the REPL and the desktop frontend."""

    user_text: str
    reply: str
    memory_notes: List[str]
    ok: bool = True
    llm_available: bool = True
    error: Optional[str] = None


def process_turn(
    text: str,
    history: List[Tuple[str, str]],
    provider: Optional[str] = None,
    mem_dir: Optional[Path] = None,
) -> TurnResult:
    """
    Classify, update memory, generate the reply.
    Does not mutate `history`: the caller appends only if `ok` and `llm_available`.
    """
    text = (text or "").strip()
    if not text:
        return TurnResult(
            user_text="",
            reply="",
            memory_notes=[],
            ok=False,
            error="empty",
        )

    client = resolve_client(provider)
    classified = classify_user_message(text, client=client)
    notes = apply_classification(classified, mem_dir=mem_dir)
    memory = load_memory(mem_dir)

    if client is None:
        err = _client_error or "LLM provider unavailable"
        return TurnResult(
            user_text=text,
            reply=(
                f"I cannot reply in chat ({err}). "
                "Configure .env / --provider. "
                "Any memory updates were still saved."
            ),
            memory_notes=notes,
            ok=False,
            llm_available=False,
            error=err,
        )

    try:
        reply = conversational_reply(client, memory, history, text, notes)
    except Exception as exc:
        return TurnResult(
            user_text=text,
            reply="",
            memory_notes=notes,
            ok=False,
            llm_available=True,
            error=str(exc),
        )

    return TurnResult(
        user_text=text,
        reply=reply,
        memory_notes=notes,
        ok=True,
        llm_available=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_client_cache: Dict[str, LLMClient] = {}
_client_error: Optional[str] = None


def resolve_client(provider: Optional[str]) -> Optional[LLMClient]:
    global _client_error
    key = provider or os.environ.get("LLM_PROVIDER") or "anthropic"
    if key in _client_cache:
        return _client_cache[key]
    try:
        client = get_llm_client(provider)
        _client_cache[key] = client
        _client_error = None
        return client
    except Exception as exc:
        _client_error = str(exc)
        return None


def _is_exit(text: str) -> bool:
    return text.strip().lower() in ("exit", "quit", "q")


def run_repl(provider: Optional[str] = None) -> None:
    mem = memory_dir()
    present = [name for name in MEMORY_FILES if (mem / name).is_file()]
    missing = [name for name in MEMORY_FILES if name not in present]
    print("OgunJob master - persistent memory in", mem)
    print("Loaded files:", ", ".join(present) if present else "(none)")
    if missing:
        print("Missing files:", ", ".join(missing))
    print('Commands: "exit" / "quit" to leave.\n')

    # Lazy client: "exit" as first input does not require an API key.
    history: List[Tuple[str, str]] = []

    while True:
        try:
            raw = input("You: ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        text = raw.strip()
        if not text:
            continue
        if _is_exit(text):
            print("Bye.")
            break

        result = process_turn(text, history, provider=provider)

        if result.memory_notes:
            print("Memory:", "; ".join(result.memory_notes))

        if not result.llm_available:
            print(f"Ogun: {result.reply}")
            continue

        if not result.ok:
            print(f"Ogun: LLM call failed ({result.error}).")
            continue

        print(f"Ogun: {result.reply}")
        history.append(("user", text))
        history.append(("assistant", result.reply))


def main() -> None:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="OgunJob conversational master agent (markdown memory + chat)"
    )
    parser.add_argument(
        "--provider",
        choices=list(SUPPORTED_PROVIDERS),
        default=None,
        help="LLM provider (overrides LLM_PROVIDER in .env). Default: anthropic",
    )
    args = parser.parse_args()
    run_repl(provider=args.provider)


if __name__ == "__main__":
    main()
