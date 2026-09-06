"""
ingest/sources.py
------------------
Loaders for the raw sources used to build the profile:
- CV text (pdf / docx / txt)
- public GitHub repositories (description + README)
- extra local documents (papers, project notes, etc.)
- ogunjob.db skills table (optional, non-blocking if missing/empty)

No LLM calls here: I/O and raw text extraction only.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import requests

GITHUB_API = "https://api.github.com"

# Agreed path: ogunjob.db at repo root (see schema.sql / project.md).
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "ogunjob.db"

# (x, font_size, text) su una stessa riga visiva
_LineFrag = Tuple[float, float, str]


def _join_line_fragments(frags: List[_LineFrag]) -> str:
    """Reassemble a line from positioned glyphs (often 1 letter per PDF operation)."""
    frags = sorted(frags, key=lambda f: f[0])
    out = ""
    prev_x: Optional[float] = None
    prev_size = 12.0
    prev_text = ""

    for x, size, text in frags:
        text = text.replace("\xa0", " ")
        if not text:
            continue
        if not out:
            out = text
            prev_x, prev_size, prev_text = x, size, text
            continue

        est_end = prev_x + prev_size * 0.4 * max(len(prev_text.strip()) or 1, 1)
        slack = prev_size * 0.2
        punct = text[:1] in ".,;:!?)]}/'" or out[-1:] in "-/([{$'"
        same_word = (
            x <= est_end + slack
            or punct
            or out.endswith(" ")
            or text.startswith(" ")
        )
        col_gap = (x - est_end) > max(40.0, prev_size * 3)

        if col_gap and not punct:
            out += "  " + text
        elif same_word:
            out += text
        else:
            out += " " + text
        prev_x, prev_size, prev_text = x, size, text
    return _tidy_line(out)


def _tidy_line(s: str) -> str:
    s = re.sub(r"\s+([,.;:)])", r"\1", s)
    s = re.sub(r"([({])\s+", r"\1", s)
    s = re.sub(r"(?<=[a-z])\s-\s(?=[a-z])", "-", s)
    s = re.sub(r"\s*\|\s*", " | ", s)
    return s.strip()


def _group_glyphs_into_lines(
    glyphs: List[Tuple[float, float, float, str]],
) -> List[Tuple[float, List[_LineFrag]]]:
    glyphs = sorted(glyphs, key=lambda g: (-g[0], g[1]))
    lines: List[Tuple[float, List[_LineFrag]]] = []
    current: List[_LineFrag] = []
    current_y: Optional[float] = None
    current_size = 12.0

    for y, x, size, text in glyphs:
        if current_y is None:
            current = [(x, size, text)]
            current_y = y
            current_size = size
            continue
        tol = max(3.0, min(current_size, size) * 0.4)
        if abs(y - current_y) <= tol:
            current.append((x, size, text))
            current_size = (current_size + size) / 2
        else:
            lines.append((current_y, current))
            current = [(x, size, text)]
            current_y = y
            current_size = size
    if current_y is not None:
        lines.append((current_y, current))
    return lines


def _detect_column_split(
    lines: List[Tuple[float, List[_LineFrag]]], page_width: float
) -> Optional[float]:
    xs = sorted({round(f[0]) for _, frags in lines for f in frags})
    if len(xs) < 20:
        return None
    best_gap = 0.0
    best_mid: Optional[float] = None
    lo, hi = page_width * 0.18, page_width * 0.65
    for a, b in zip(xs, xs[1:]):
        if a < lo or b > hi:
            continue
        gap = b - a
        if gap > best_gap:
            best_gap = gap
            best_mid = (a + b) / 2
    if best_gap < 28:
        return None
    return best_mid


def _rejoin_fragmented_lines(lines: List[str], char_mode: bool) -> str:
    paragraphs: List[str] = []
    words: List[str] = []
    char_buf: List[str] = []
    blank_run = 0

    def flush_word() -> None:
        if char_buf:
            words.append("".join(char_buf))
            char_buf.clear()

    def flush_paragraph() -> None:
        flush_word()
        if words:
            paragraphs.append(" ".join(words))
            words.clear()

    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            blank_run += 1
            if char_mode:
                flush_word()
                if blank_run >= 2:
                    flush_paragraph()
            else:
                flush_paragraph()
            continue
        blank_run = 0
        if char_mode:
            char_buf.append(stripped)
        else:
            words.append(stripped)
    flush_paragraph()
    return "\n\n".join(paragraphs)


def _collapse_blank_lines(text: str) -> str:
    text = (
        text.replace("\xa0", " ")
        .replace("\x00", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    raw_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    out: List[str] = []
    pending_blank = False
    for ln in raw_lines:
        if not ln:
            pending_blank = True
            continue
        if pending_blank and out:
            out.append("")
        pending_blank = False
        out.append(ln)
    return "\n".join(out).strip()


def _normalize_extracted_text(text: str) -> str:
    """Collapse spurious spaces/newlines; if text is 1 letter/word per line, rejoin it."""
    text = (
        text.replace("\xa0", " ")
        .replace("\x00", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    raw_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    nonempty = [ln for ln in raw_lines if ln]
    if len(nonempty) >= 8:
        single = sum(1 for ln in nonempty if len(ln) == 1) / len(nonempty)
        avg = sum(len(ln) for ln in nonempty) / len(nonempty)
        if single >= 0.4:
            return _rejoin_fragmented_lines(raw_lines, char_mode=True)
        if avg <= 18:
            return _rejoin_fragmented_lines(raw_lines, char_mode=False)
    return _collapse_blank_lines(text)


def _extract_pdf_page_text(page) -> str:
    """
    Extract text from a PDF page by rebuilding lines from coordinates.
    Default extract_text(), on CVs exported from Canva/Figma/etc., produces
    one letter (or one word) per line — unusable in dry-run and for the LLM.
    """
    glyphs: List[Tuple[float, float, float, str]] = []

    def visitor_text(text, cm, tm, font_dict, font_size) -> None:
        if not text:
            return
        cleaned = text.replace("\n", "").replace("\r", "").replace("\xa0", " ")
        if cleaned == "":
            return
        if not cleaned.strip() and cleaned != " ":
            return
        glyphs.append((float(tm[5]), float(tm[4]), float(font_size or 12), cleaned))

    try:
        page.extract_text(visitor_text=visitor_text)
    except TypeError:
        glyphs = []

    useful = [(y, x, s, t) for y, x, s, t in glyphs if t.strip() or t == " "]
    if not useful:
        return _normalize_extracted_text(page.extract_text() or "")

    lines = _group_glyphs_into_lines(useful)
    split = _detect_column_split(lines, float(page.mediabox.width))
    if split is None:
        body = "\n".join(_join_line_fragments(frags) for _, frags in lines)
    else:
        left: List[str] = []
        right: List[str] = []
        for _, frags in lines:
            lf = [f for f in frags if f[0] < split]
            rf = [f for f in frags if f[0] >= split]
            if lf:
                left.append(_join_line_fragments(lf))
            if rf:
                right.append(_join_line_fragments(rf))
        body = "\n".join(left) + "\n\n" + "\n".join(right)
    return _collapse_blank_lines(body)


def load_db_skills(db_path: Optional[Union[str, Path]] = None) -> List[Dict]:
    """
    Read the `skills` table in ogunjob.db as extra context for extraction.

    Non-blocking: if the file is missing, the table is missing, or it is empty,
    returns []. Read-only, no CREATE TABLE (the table belongs to the matcher).
    """
    path = Path(db_path) if db_path is not None else DB_PATH
    if not path.exists():
        return []

    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT label, tags, source, confidence FROM skills"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    skills: List[Dict] = []
    for row in rows:
        label = (row["label"] or "").strip()
        if not label:
            continue
        tags_raw = row["tags"]
        tags: List[str] = []
        if tags_raw:
            try:
                parsed = json.loads(tags_raw)
                if isinstance(parsed, list):
                    tags = [str(t) for t in parsed]
                elif isinstance(parsed, str) and parsed.strip():
                    tags = [parsed.strip()]
            except (json.JSONDecodeError, TypeError):
                tags = [str(tags_raw)]
        skills.append(
            {
                "label": label,
                "tags": tags,
                "source": (row["source"] or "").strip(),
                "confidence": row["confidence"],
            }
        )
    return skills


def load_text_file(path: str) -> str:
    """Estrae testo da pdf, docx o file di testo semplice."""
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        pages = [_extract_pdf_page_text(page) for page in reader.pages]
        return "\n\n".join(t for t in pages if t)

    if suffix == ".docx":
        import docx
        doc = docx.Document(str(p))
        return "\n".join(para.text for para in doc.paragraphs)

    return p.read_text(encoding="utf-8")


def load_local_docs(paths: List[str], max_chars: int = 8000) -> List[Dict]:
    """Load extra documents (papers, notes), truncating so context does not explode."""
    docs = []
    for path in paths:
        p = Path(path)
        if not p.exists():
            print(f"WARNING: {path} not found, skipping.")
            continue
        text = load_text_file(str(p))
        docs.append({"name": p.name, "text": text[:max_chars]})
    return docs


def fetch_github_repos(username: str, max_repos: int = 15) -> List[Dict]:
    """
    Fetch public (non-fork) repositories for a GitHub user,
    with description, primary language, and a README excerpt.

    If GITHUB_TOKEN is set, it is used to authenticate requests:
    the anonymous 60/h limit rises to 5000/h, useful if you later
    want to include private repos too.
    """
    import os

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ogunjob-profile-builder",  # required by the GitHub API, otherwise 403
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(
        f"{GITHUB_API}/users/{username}/repos",
        params={"sort": "updated", "per_page": max_repos},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    repos = [r for r in resp.json() if not r.get("fork")]

    results = []
    for r in repos:
        readme_text = ""
        try:
            readme_headers = {
                "Accept": "application/vnd.github.raw+json",
                "User-Agent": "ogunjob-profile-builder",
            }
            if token:
                readme_headers["Authorization"] = f"Bearer {token}"
            readme_resp = requests.get(
                f"{GITHUB_API}/repos/{username}/{r['name']}/readme",
                headers=readme_headers,
                timeout=10,
            )
            if readme_resp.status_code == 200:
                readme_text = readme_resp.text[:4000]
        except requests.RequestException:
            pass

        results.append(
            {
                "name": r["name"],
                "description": r.get("description") or "",
                "language": r.get("language") or "",
                "stars": r.get("stargazers_count", 0),
                "url": r.get("html_url"),
                "readme_excerpt": readme_text,
            }
        )
    return results
