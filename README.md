# Ogun

Local toolkit for job search: collect listings from public feeds, score them against your profile, generate a tailored CV and cover letter, and track applications.

It is a personal tool that runs on your machine and, at the same time, a portfolio piece: LLMs are used only where context isolation matters (extraction from raw sources; writing vs evaluation). Everything else is deterministic code.

**Two human gates are part of the design**, not a gap in automation: after profile extraction (before using `cv_master.yaml`) and after CV/cover letter (before any send). No automatic submissions to job boards.

The architecture contract (flows, stores, SQLite schema, constraints for anyone extending the frontend) lives in [`project.md`](project.md).

## What it does

```
CV / GitHub  →  extract  →  [human]  →  cv_master.yaml
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

- **Swarm** — parallel fetch (`asyncio.gather`) from Remote OK (JSON) and We Work Remotely (RSS). No LLM, no scraping beyond public feeds/APIs.
- **Extract** — LLM → draft YAML from a CV (pdf/docx/txt) and public GitHub repos. Does not overwrite the master.
- **Matcher** — lexical score + optional LLM (0.0–1.0), with a cap against invented skills.
- **Author / Evaluator** — evaluator-optimizer loop: draft, score 0–100, targeted revision. Pass threshold 75, max 3 rounds.
- **Tracker** — applications in local SQLite (`ogunjob.db`).
- **Master / frontend** — chat over `memory/*.md` (criteria, decisions). It does not yet orchestrate scrape / match / author.

`python pipeline.py` is the end-to-end verification orchestrator: it runs steps in sequence and checks that each component responds.

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # Windows: copy .env.example .env
cp config/cv_master.yaml.example config/cv_master.yaml
cp memory/profile.md.example memory/profile.md
cp memory/search-criteria.md.example memory/search-criteria.md
cp memory/decisions-log.md.example memory/decisions-log.md
```

Open `.env` and fill in the placeholders:

| Variable | Role |
|----------|------|
| `LLM_PROVIDER` | `anthropic` (default), `lmstudio`, or `deepseek` |
| `ANTHROPIC_API_KEY` | required when using Anthropic |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | used only with DeepSeek |
| `LMSTUDIO_BASE_URL` / `LMSTUDIO_MODEL` | used only with LM Studio |
| `GITHUB_TOKEN` | optional; raises the GitHub rate limit from 60 to 5000 req/h |

The CLI flag `--provider` overrides `LLM_PROVIDER`.

**LM Studio:** start the local server (default `http://localhost:1234/v1`) and set `LMSTUDIO_MODEL` to the loaded model id.

**DeepSeek:** prompts must ask for JSON (`response_format: json_object`). V4 thinking stays off unless `DEEPSEEK_THINKING=1`.

Then:

1. Fill `config/cv_master.yaml` with your data (or generate a draft with extract and copy only the reviewed blocks).
2. Put a raw CV at `resources/base_cv.pdf` if you want to run extract.

Personal files (`cv_master.yaml`, extract draft, CV PDF, `memory/profile.md` and search logs) are in `.gitignore`. The repo keeps only the `.example` templates.

## Pipeline

```bash
python pipeline.py all
```

Step order: **check → scrape → match → extract → author → track**. You can run a single step, or a sequence (`python pipeline.py scrape match`).

| Command | Description |
|---------|-------------|
| `python pipeline.py all` | Runs every step in order and prints an OK/FAIL summary. |
| `python pipeline.py check` | Environment, dependencies, `.env`, `cv_master.yaml`, memory files, module imports, SQLite schema. |
| `python pipeline.py scrape` | Swarm scraper → `jobs` table (no LLM). Fails if the DB still has no listings. |
| `python pipeline.py match` | Profile/listing scoring → `matches` table. |
| `python pipeline.py extract` | Profile draft from CV (and GitHub if provided) → `config/master_profile_draft.yaml`. |
| `python pipeline.py author` | Author/Evaluator loop: tailored CV + cover letter in `--out`. |
| `python pipeline.py track` | Tracker smoke: test `add`, then `list` and `stats`. |

### Extra flags

| Flag | Description |
|------|-------------|
| `--smoke` | Light test: with `all`, skips extract/author LLM; match runs lexical-only with low limits. |
| `--ping-llm` | In the check step, send a ping to the configured LLM provider. |
| `--provider` | Override `LLM_PROVIDER` for match, extract, author, and check `--ping-llm`. |
| `--keywords` | Comma-separated scrape keywords. Default: the swarm defaults. |
| `--no-llm` | Lexical-only match (match step). |
| `--match-limit` | Cap how many listings are scored in the match step. |
| `--match-force` | Recompute matches that already exist. |
| `--cv` | CV path for extract. Default: `resources/base_cv.pdf`. |
| `--github` | Optional GitHub username for extract. |
| `--extract-dry-run` | Extract without an LLM call (assemble input only). |
| `--job` | Job-ad text file for author. Default: `output/_loop_test/job_ad.txt`. |
| `--out` | Author output directory. Default: `output/pipeline_run`. |
| `--max-iterations` | Author/Evaluator rounds (default 2; `--smoke` drops this to 1). |
| `--skip-track-add` | Track: `list`/`stats` only, no test INSERT. |

## Real workflow (beyond the pipeline)

The pipeline checks that the pieces respond. For real use the human gates stay:

1. **Extract** — `python build_profile.py --cv resources/base_cv.pdf --github USER` writes `config/master_profile_draft.yaml`. Review `needs_review` blocks, copy facts into `cv_master.yaml`, then `python build_profile.py --commit-to-memory`.
2. **Scrape + match** — `python -m scrapers.swarm` then `python -m matcher.matcher --force`. Top matches: `python -m matcher.matcher --top 5`.
3. **Author** — `python -m author.loop --job output/job_<id>.txt --out output/job_<id>/` (alias: `python tailor.py`). Re-read `cv_tailored.md` and `cover_letter.md` before sending.
4. **Tracker** — `python tracker.py add --company "…" --role "…"`; then `list`, `update`, `stats`.
5. **Master** — `python master.py` or `python -m frontend` for chat over the memory files.

Stores: `config/cv_master.yaml` (factual source of truth), `memory/*.md` (criteria and decisions), `ogunjob.db` (listings, scores, applications). Do not mix them. DDL in [`schema.sql`](schema.sql).
