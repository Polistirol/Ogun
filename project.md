# OgunJob — project.md

Architecture contract and overview for anyone extending the product
(especially the frontend). Describes the system **as it is in the code today**,
not the old task 1–5 roadmap (that work is closed).

`README.md` is the user guide (setup, commands). This file is the contract:
flows, data, who writes where, what the frontend may call.

## Vision

Personal local tool to automate job search (tailored CV, listing matching,
application tracking). Also a portfolio piece: motivated agentic patterns,
not “many agents for the sake of it”.

## Architectural principle

- **Not a mega-prompt with 15 tools**, and **not a swarm of LLM agents
  for every function**. An LLM is justified only by context/persona
  isolation (extraction from raw sources; writing vs evaluation).
  Everything else is deterministic code.
- **Swarm = tool parallelization** (`asyncio.gather`), not thinking agents.
  Fetch and normalization = HTTP/parse; matching happens downstream.
- **Loop = evaluator-optimizer** (Author → Evaluator → targeted revision),
  not a free-form dialogue between agents.
- **Persistent memory in Markdown** (readable, git, inspectable by eye).
  No vector DB.
- **Mandatory human review** at two points: after profile extraction
  (before `cv_master.yaml`) and after CV/cover letter (before sending).
  That is not an automation hole: it is design. The frontend must expose
  these gates, not bypass them.

## User flow (real order)

```
CV / GitHub  →  extract  →  [human]  →  cv_master.yaml (+ opt. memory/profile.md)
                                          ↓
public listings  →  swarm  →  jobs  →  matcher  →  matches
                                          ↓
                              [human picks a job]
                                          ↓
                              author.loop  →  output/job_<id>/
                                          ↓
                              [human reviews]
                                          ↓
                              tracker  →  applications
```

The master (`master.py` / `python -m frontend`) is chat over `memory/*.md`
(criteria, decisions). It does **not yet orchestrate** scrape / extract /
match / author: `AVAILABLE_TOOLS` exists but every `handler` is `None`.

`python pipeline.py all` is a **smoke/verification run**, not the product
flow: it skips human gates and uses an order (scrape before extract) that
is wrong for a real run.

## Component map

```
ogunjob/
├── project.md                  this file
├── schema.sql                  canonical DDL for ogunjob.db
├── .env / .env.example
├── llm_provider.py             get_llm_client() — anthropic | lmstudio | deepseek
├── memory/
│   ├── profile.md              stable facts (post-review extract)
│   ├── search-criteria.md      criteria; written by master in chat
│   ├── market-insights.md      intended post scrape+match; NOT auto-updated yet
│   └── decisions-log.md        decisions; append-only from master
├── master.py                   REPL + process_turn() for the frontend
├── frontend/                   desktop PySide6 — chat master only today
│   ├── app.py
│   └── __main__.py             python -m frontend
├── scrapers/
│   ├── _common.py              HTTP, keywords, filter
│   ├── remoteok.py             public JSON
│   ├── weworkremotely.py       Programming RSS
│   └── swarm.py                asyncio.gather → jobs table
├── ingest/
│   ├── sources.py              I/O: CV pdf/docx/txt, GitHub, skills DB (read)
│   └── extract.py              LLM → JSON; commit_validated_to_memory()
├── build_profile.py            extract CLI + --commit-to-memory
├── author/
│   ├── author.py               generate_draft / revise_draft
│   ├── evaluator.py            score 0–100, threshold 75
│   └── loop.py                 orchestrator + CLI
├── tailor.py                   wrapper: calls author.loop.main
├── matcher/
│   └── matcher.py              lexical + LLM, anti-invention cap
├── tracker.py                  applications CLI
├── pipeline.py                 smoke/verification orchestrator (not UI)
├── config/
│   ├── cv_master.yaml          curated source for Author/matcher — never auto-overwrite
│   └── master_profile_draft.yaml  extract draft (do not use as-is)
├── resources/base_cv.pdf       raw CV for extract
├── output/                     tailored CV, cover letter, job_*.txt
└── ogunjob.db                  local SQLite (.gitignore)
```

Also: `README.md`, `requirements.txt` (includes `PySide6-Essentials`).

## Three stores — do not mix them

| Store | Who writes | Who reads | Role |
|-------|------------|-----------|------|
| `config/cv_master.yaml` | human (after extract) | Author, matcher | factual truth for tailored CVs |
| `config/master_profile_draft.yaml` | `build_profile.py` | human, then `--commit-to-memory` | draft with `needs_review` |
| `memory/*.md` | master (criteria/decisions); extract only via `--commit-to-memory` on `profile.md` | master chat, matcher (`profile.md` optional) | working memory |
| `ogunjob.db` | swarm, matcher, tracker | matcher, tracker, extract (skills, read-only) | listings, scores, applications |

Extract does **not** overwrite `cv_master.yaml`. The master does **not**
write `profile.md` in chat.

## Data contract — `ogunjob.db`

One file at repo root. DDL in `schema.sql`. Writers run their own
`CREATE TABLE IF NOT EXISTS` (same SQL). No `applications.db`.

Paths:

```python
from pathlib import Path
# package (scrapers/, matcher/, ingest/):
DB_PATH = Path(__file__).resolve().parent.parent / "ogunjob.db"
# root (tracker.py, pipeline.py):
DB_PATH = Path(__file__).resolve().parent / "ogunjob.db"
```

### `applications` — frozen (`tracker.py`)

| column | type | notes |
|--------|------|-------|
| id | INTEGER PK AUTOINCREMENT | |
| company | TEXT NOT NULL | |
| role | TEXT NOT NULL | |
| link | TEXT | |
| cv_version | TEXT | e.g. output folder `job_9` |
| status | TEXT DEFAULT `'da_inviare'` | free text |
| applied_on | TEXT | ISO date |
| last_update | TEXT | ISO date |
| notes | TEXT | |

Typical statuses: `da_inviare` → `applied` → `screening` → `colloquio_1` → …

### `jobs` — swarm

| column | type | notes |
|--------|------|-------|
| id | INTEGER PK AUTOINCREMENT | `job_id` used by matches and UI |
| source | TEXT NOT NULL | `weworkremotely`, `remoteok` |
| title | TEXT NOT NULL | |
| company | TEXT | |
| url | TEXT NOT NULL | |
| description | TEXT | |
| location | TEXT | |
| remote | INTEGER NOT NULL DEFAULT 0 | `0`/`1` |
| posted_date | TEXT | ISO date or NULL |
| scraped_at | TEXT NOT NULL | ISO datetime |
| raw_json | TEXT | source payload |

`UNIQUE (source, url)`. Indexes: `source`, `scraped_at`. Re-scrape =
`INSERT OR IGNORE` (count “new”, not duplicates).

### `skills` — matcher

| column | type | notes |
|--------|------|-------|
| id | INTEGER PK AUTOINCREMENT | |
| label | TEXT NOT NULL | |
| tags | TEXT | JSON array |
| source | TEXT | `cv` or `job:<id>` |
| confidence | REAL | `0.0`–`1.0` or NULL |

`UNIQUE (label, source)`.

### `matches` — matcher

One **current** match per job (UPSERT on `job_id`).

| column | type | notes |
|--------|------|-------|
| id | INTEGER PK AUTOINCREMENT | |
| job_id | INTEGER NOT NULL | logical → `jobs.id`, **no FK** |
| score | REAL NOT NULL | **`0.0`–`1.0`**, not 0–100 |
| rationale | TEXT | |
| computed_at | TEXT | ISO datetime |

`UNIQUE (job_id)`. Index `score DESC`.

## How to call each step (CLI and Python)

The frontend must **import functions**, not launch `pipeline.py` via
subprocess. CLIs remain for terminal use.

| UI step | CLI | Import |
|---------|-----|--------|
| Master chat | `python master.py` / `python -m frontend` | `from master import process_turn, load_memory, AVAILABLE_TOOLS` |
| Extract preview | `python build_profile.py --cv … --dry-run` | `ingest.extract.extract_profile(..., dry_run=True)` |
| Extract profile | `python build_profile.py --cv resources/base_cv.pdf --github USER` | `ingest.sources.load_text_file`, `extract_profile`, then YAML at `config/master_profile_draft.yaml` |
| Commit memory | `python build_profile.py --commit-to-memory` | `ingest.extract.commit_validated_to_memory` — only after `needs_review: false` |
| Scrape | `python -m scrapers.swarm` | `scrapers.swarm.run_swarm`, `write_jobs` |
| Match | `python -m matcher.matcher --force` | `matcher.matcher.score_jobs`, `write_matches` |
| Top match | `python -m matcher.matcher --top 5` | `matcher.matcher.fetch_top` / `print_top` |
| Export listing | *no CLI* | read `jobs` and write `output/job_<id>.txt` — wrap this |
| Generate CV | `python -m author.loop --job FILE --out DIR` | `author.run_loop`; output: `cv_tailored.md`, `cover_letter.md`, `evaluation.json` |
| Tracker | `python tracker.py add\|list\|update\|stats` | today `cmd_*` tied to argparse — wrap as functions |

Master REPL exit: `exit` / `quit` / `q`.

Author: evaluator threshold **75/100**, max **3** rounds (`--max-iterations`).
Matcher: score **0.0–1.0**; without `--force` only jobs with no match are scored.
Extract: `--no-db-skills` if `skills` is empty (before the matcher).

## Frontend — status and constraints

Exists: `python -m frontend` (PySide6). Chat + memory tabs + provider combo.
Calls `process_turn()` on a `QThread`.

Not in the UI yet: scrape, extract, match, job list, generate CV, tracker.
The “TOOL” box shows the master registry, all **unwired**.

Constraints for implementers:

1. **Two human gates in UI**: review the extract draft (before using
   `cv_master.yaml`); review `cv_tailored.md` / cover letter (no
   “submit application” button).
2. **No automatic send** to job boards.
3. Wire steps by **importing the modules** above, not by duplicating SQL
   or prompts. Do not change `schema.sql` without agreement.
4. `dispatch_tool` in `master.py` is the chat→tool hook; today it raises
   `NotImplementedError`. The UI may call modules directly without going
   through the master.
5. LLM work in a thread/worker (already done for chat): scrape/match/
   author are long I/O.
6. `tailor.py` is only an alias of `author.loop`; new UI should target `author`.

## LLM providers

`get_llm_client(provider=None)`: CLI argument > env `LLM_PROVIDER` >
default `anthropic`. Values: `anthropic` | `lmstudio` | `deepseek`.

Helpers: `log_llm_start`, `log_llm_usage`, `parse_json_object`,
`SUPPORTED_PROVIDERS`.

DeepSeek forces `response_format: json_object` (prompts must contain
“json”; V4 thinking off unless `DEEPSEEK_THINKING=1`).
Do not remove DeepSeek from the interface.

Swarm: **no LLM**. Matcher: optional LLM (`--no-llm` = lexical only).

## Memory — `memory/*.md`

Header with `last_updated`, `updated_by`, who updates it.

- `profile.md` — after extract review (`--commit-to-memory`).
- `search-criteria.md` — master in conversation.
- `market-insights.md` — **intended** after scrape+match; the matcher
  does **not** write it yet.
- `decisions-log.md` — append-only from the master.

## Roadmap / status (real code)

- [x] `llm_provider.py`, `schema.sql`, `ogunjob.db`
- [x] `tracker.py` — `applications`
- [x] `memory/*.md` + `master.py` (`process_turn` for UI)
- [x] `scrapers/` async swarm → `jobs`
- [x] extract + `--commit-to-memory` (master YAML stays manual)
- [x] `author/` Author↔Evaluator loop; `tailor.py` wrapper
- [x] `matcher/` lexical + LLM, `skills` / `matches` tables
- [x] `frontend/` master chat (PySide6)
- [ ] UI: scrape, extract, match, export job, author, tracker
- [ ] wire `AVAILABLE_TOOLS` / `dispatch_tool`
- [ ] write `market-insights.md` after scrape+match
- [ ] (phase 2) knowledge graph; best-of-n CV variants

## Explicit non-goals

- No automatic application submissions
- No free-form dialogue/negotiation between agents
- No scraping beyond public feeds/APIs (RSS, JSON)
- No vector DB / knowledge graph in this version
- No invented SQLite schema: copy `schema.sql`
- No automatic overwrite of `config/cv_master.yaml`
