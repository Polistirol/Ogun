#!/usr/bin/env python3
"""
pipeline.py
-----------
End-to-end orchestrator for OgunJob: runs steps in sequence and checks
that each component responds correctly.

Recommended order (aligned with the real flow):
  1. check    — environment, imports, memory, DB schema, LLM provider
  2. scrape   — swarm scraper → jobs table (no LLM)
  3. match    — profile/listing scoring → matches table
  4. extract  — profile draft from CV (optional, needs --cv)
  5. author   — CV + cover letter with Author/Evaluator loop
  6. track    — application tracker smoke test

Examples:
  python pipeline.py check
  python pipeline.py all --smoke
  python pipeline.py scrape match --no-llm --match-limit 5
  python pipeline.py author --job output/_loop_test/job_ad.txt --out output/pipeline_test
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "ogunjob.db"
SCHEMA_PATH = ROOT / "schema.sql"
ENV_PATH = ROOT / ".env"
CV_MASTER = ROOT / "config" / "cv_master.yaml"
DEFAULT_JOB_AD = ROOT / "output" / "_loop_test" / "job_ad.txt"
DEFAULT_CV = ROOT / "resources" / "base_cv.pdf"

MEMORY_FILES = (
    "profile.md",
    "search-criteria.md",
    "market-insights.md",
    "decisions-log.md",
)

REQUIRED_PACKAGES = (
    "yaml",
    "dotenv",
    "anthropic",
    "openai",
    "requests",
)

STEP_ORDER = ("check", "scrape", "match", "extract", "author", "track")


@dataclass
class StepResult:
    name: str
    ok: bool
    summary: str
    details: List[str] = field(default_factory=list)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _banner(title: str) -> None:
    line = "=" * 60
    _p(f"\n{line}\n  {title}\n{line}")


def _run_subprocess(cmd: Sequence[str], *, label: str) -> StepResult:
    _p(f"\n$ {' '.join(cmd)}")
    proc = subprocess.run(
        list(cmd),
        cwd=ROOT,
        capture_output=False,
        text=True,
    )
    ok = proc.returncode == 0
    return StepResult(
        name=label,
        ok=ok,
        summary=f"exit {proc.returncode}",
        details=[f"comando: {' '.join(cmd)}"],
    )


def _table_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for table in tables:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return counts
    finally:
        conn.close()


def _init_db_from_schema(db_path: Path) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def step_check(*, ping_llm: bool = False, provider: Optional[str] = None) -> StepResult:
    _banner("STEP check — environment and prerequisites")
    details: List[str] = []
    ok = True

    details.append(f"Python {sys.version.split()[0]} @ {ROOT}")

    missing_pkg: List[str] = []
    for pkg in REQUIRED_PACKAGES:
        mod = "yaml" if pkg == "yaml" else pkg.replace("-", "_")
        try:
            importlib.import_module(mod)
        except ImportError:
            missing_pkg.append(pkg)
    if missing_pkg:
        ok = False
        details.append(f"Missing packages: {', '.join(missing_pkg)} (pip install -r requirements.txt)")
    else:
        details.append("Python dependencies: OK")

    if not ENV_PATH.exists():
        details.append("WARNING: .env missing — copy .env.example and fill in the keys")
    else:
        details.append(".env present")

    if not CV_MASTER.exists():
        ok = False
        details.append(f"Missing: {CV_MASTER.relative_to(ROOT)}")
    else:
        details.append(f"CV master: {CV_MASTER.relative_to(ROOT)}")

    mem_dir = ROOT / "memory"
    for name in MEMORY_FILES:
        path = mem_dir / name
        if path.exists():
            details.append(f"memory/{name}: OK ({path.stat().st_size} B)")
        else:
            ok = False
            details.append(f"memory/{name}: MISSING")

    modules = (
        "llm_provider",
        "tracker",
        "master",
        "build_profile",
        "scrapers.swarm",
        "matcher.matcher",
        "author.loop",
        "ingest.extract",
    )
    for mod in modules:
        try:
            importlib.import_module(mod)
            details.append(f"import {mod}: OK")
        except Exception as exc:
            ok = False
            details.append(f"import {mod}: ERROR — {exc}")

    _init_db_from_schema(DB_PATH)
    counts = _table_counts(DB_PATH)
    details.append(f"DB {DB_PATH.name}: {counts or 'empty'}")

    if ping_llm:
        try:
            from llm_provider import get_llm_client, log_llm_usage

            client = get_llm_client(provider)
            details.append(f"Provider LLM: {client.provider} / {client.model}")
            result = client.complete(
                system="Reply with valid JSON: {\"status\":\"OK\"}.",
                user="Pipeline ping: return json with status OK.",
                max_tokens=32,
            )
            log_llm_usage(result)
            text = (result.text or "").strip()
            details.append(f"LLM ping: reply={text!r}")
            if not text:
                ok = False
                details.append("LLM ping: empty reply")
        except Exception as exc:
            ok = False
            details.append(f"LLM ping failed: {exc}")
    else:
        details.append("LLM ping: skipped (use --ping-llm to verify the provider)")

    for line in details:
        mark = "✓" if not line.startswith(
            ("WARNING", "MISSING", "ERROR", "failed", "Missing", "empty reply")
        ) else "!"
        _p(f"  {mark} {line}")

    return StepResult(
        name="check",
        ok=ok,
        summary="OK" if ok else "issues found",
        details=details,
    )


def step_scrape(*, keywords: Optional[str] = None) -> StepResult:
    _banner("STEP scrape — swarm → jobs")
    cmd = [sys.executable, "-m", "scrapers.swarm"]
    if keywords:
        cmd.extend(["--keywords", keywords])
    result = _run_subprocess(cmd, label="scrape")
    counts = _table_counts(DB_PATH)
    jobs = counts.get("jobs", 0)
    result.details.append(f"jobs in DB: {jobs}")
    if jobs == 0:
        result.ok = False
        result.summary = "no listings in DB"
    else:
        result.summary = f"{jobs} listings in DB"
    _p(f"\n  → {result.summary}")
    return result


def step_match(
    *,
    no_llm: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
    provider: Optional[str] = None,
) -> StepResult:
    _banner("STEP match — profilo/annunci → matches")
    cmd = [sys.executable, "-m", "matcher.matcher"]
    if no_llm:
        cmd.append("--no-llm")
    if limit is not None:
        cmd.extend(["--limit", str(limit)])
    if force:
        cmd.append("--force")
    if provider:
        cmd.extend(["--provider", provider])
    result = _run_subprocess(cmd, label="match")
    counts = _table_counts(DB_PATH)
    matches = counts.get("matches", 0)
    result.details.append(f"matches in DB: {matches}")
    if matches == 0 and counts.get("jobs", 0) > 0:
        result.ok = False
        result.summary = "jobs present but no match written"
    else:
        result.summary = f"{matches} match in DB"
    _p(f"\n  → {result.summary}")
    return result


def step_extract(
    *,
    cv: Path,
    github: Optional[str] = None,
    dry_run: bool = False,
    provider: Optional[str] = None,
) -> StepResult:
    _banner("STEP extract — CV/GitHub → YAML draft")
    if not cv.exists():
        return StepResult(
            name="extract",
            ok=False,
            summary=f"CV not found: {cv}",
        )
    cmd = [sys.executable, "build_profile.py", "--cv", str(cv), "--no-db-skills"]
    if github:
        cmd.extend(["--github", github])
    if dry_run:
        cmd.append("--dry-run")
    if provider:
        cmd.extend(["--provider", provider])
    result = _run_subprocess(cmd, label="extract")
    draft = ROOT / "config" / "master_profile_draft.yaml"
    if dry_run:
        result.summary = "dry-run completed (no draft written)"
    elif draft.exists():
        result.summary = f"draft at {draft.relative_to(ROOT)}"
        result.details.append(f"size: {draft.stat().st_size} B")
    else:
        result.ok = False
        result.summary = "YAML draft not generated"
    _p(f"\n  → {result.summary}")
    return result


def step_author(
    *,
    job: Path,
    out: Path,
    provider: Optional[str] = None,
    max_iterations: int = 2,
) -> StepResult:
    _banner("STEP author — loop Author/Evaluator")
    if not job.exists():
        return StepResult(
            name="author",
            ok=False,
            summary=f"job ad not found: {job}",
        )
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "author.loop",
        "--job",
        str(job),
        "--out",
        str(out),
        "--max-iterations",
        str(max_iterations),
    ]
    if provider:
        cmd.extend(["--provider", provider])
    result = _run_subprocess(cmd, label="author")
    expected = ("cv_tailored.md", "cover_letter.md", "raw_output.json", "evaluation.json")
    missing = [name for name in expected if not (out / name).exists()]
    if missing:
        result.ok = False
        result.summary = f"incomplete output, missing: {', '.join(missing)}"
    else:
        result.summary = f"output in {out.relative_to(ROOT)}"
    _p(f"\n  → {result.summary}")
    return result


def step_track(*, smoke_add: bool = True) -> StepResult:
    _banner("STEP track — application tracker")
    details: List[str] = []
    ok = True

    if smoke_add:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        add_cmd = [
            sys.executable,
            "tracker.py",
            "add",
            "--company",
            "PipelineTest",
            "--role",
            f"Smoke {stamp}",
            "--link",
            "https://example.com/pipeline-smoke",
            "--cv-version",
            "pipeline_smoke",
            "--status",
            "test",
        ]
        add_result = _run_subprocess(add_cmd, label="track-add")
        ok = ok and add_result.ok
        details.extend(add_result.details)

    for sub in ("list", "stats"):
        sub_result = _run_subprocess(
            [sys.executable, "tracker.py", sub],
            label=f"track-{sub}",
        )
        ok = ok and sub_result.ok
        details.extend(sub_result.details)

    counts = _table_counts(DB_PATH)
    apps = counts.get("applications", 0)
    summary = f"{apps} applications in DB"
    _p(f"\n  → {summary}")
    return StepResult(name="track", ok=ok, summary=summary, details=details)


def _print_summary(results: Sequence[StepResult]) -> int:
    _banner("PIPELINE SUMMARY")
    all_ok = True
    for r in results:
        mark = "OK" if r.ok else "FAIL"
        _p(f"  [{mark}] {r.name}: {r.summary}")
        all_ok = all_ok and r.ok
    _p()
    if all_ok:
        _p("Pipeline completed successfully.")
        return 0
    _p("Pipeline finished with errors — check the FAIL steps above.")
    return 1


def _resolve_steps(
    requested: Sequence[str],
    *,
    smoke: bool,
) -> List[str]:
    if "all" in requested:
        if smoke:
            return ["check", "scrape", "match", "track"]
        return list(STEP_ORDER)
    out: List[str] = []
    for name in requested:
        if name not in STEP_ORDER:
            raise SystemExit(
                f"Unknown step: {name!r}. Valid: {', '.join(STEP_ORDER)}, all"
            )
        if name not in out:
            out.append(name)
    return out


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OgunJob end-to-end pipeline with step-by-step checks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            f"""
            Step order: {' → '.join(STEP_ORDER)}

            Quick modes:
              python pipeline.py check
              python pipeline.py all --smoke
              python pipeline.py scrape match track --no-llm --match-limit 5
            """
        ),
    )
    parser.add_argument(
        "steps",
        nargs="+",
        metavar="STEP",
        help=f"One or more steps ({', '.join(STEP_ORDER)}) or 'all'.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Light test: skip extract/author LLM; match with --no-llm and low limits.",
    )
    parser.add_argument(
        "--ping-llm",
        action="store_true",
        help="In the check step, send a ping to the configured LLM provider.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Override LLM_PROVIDER for match/extract/author/check --ping-llm.",
    )
    parser.add_argument(
        "--keywords",
        default=None,
        help="Comma-separated scrape keywords. Default: the swarm defaults.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Lexical-only match (match step).",
    )
    parser.add_argument(
        "--match-limit",
        type=int,
        default=None,
        help="Cap how many listings are scored in the match step.",
    )
    parser.add_argument(
        "--match-force",
        action="store_true",
        help="Recompute matches that already exist.",
    )
    parser.add_argument(
        "--cv",
        type=Path,
        default=DEFAULT_CV,
        help=f"CV for extract (default: {DEFAULT_CV.relative_to(ROOT)}).",
    )
    parser.add_argument(
        "--github",
        default=None,
        help="Optional GitHub username for extract.",
    )
    parser.add_argument(
        "--extract-dry-run",
        action="store_true",
        help="Extract without an LLM call (assemble input only).",
    )
    parser.add_argument(
        "--job",
        type=Path,
        default=DEFAULT_JOB_AD,
        help=f"Job ad for author (default: {DEFAULT_JOB_AD.relative_to(ROOT)}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "output" / "pipeline_run",
        help="Author output directory.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=2,
        help="Author/Evaluator rounds (default 2; --smoke uses 1).",
    )
    parser.add_argument(
        "--skip-track-add",
        action="store_true",
        help="Track: list/stats only, no test INSERT.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    steps = _resolve_steps(args.steps, smoke=args.smoke)

    if args.smoke:
        if args.match_limit is None:
            args.match_limit = 5
        if args.max_iterations > 1:
            args.max_iterations = 1

    dispatch: dict[str, Callable[[], StepResult]] = {
        "check": lambda: step_check(
            ping_llm=args.ping_llm or (not args.smoke and "author" in steps),
            provider=args.provider,
        ),
        "scrape": lambda: step_scrape(keywords=args.keywords),
        "match": lambda: step_match(
            no_llm=args.no_llm or args.smoke,
            limit=args.match_limit,
            force=args.match_force or args.smoke,
            provider=args.provider,
        ),
        "extract": lambda: step_extract(
            cv=args.cv,
            github=args.github,
            dry_run=args.extract_dry_run or args.smoke,
            provider=args.provider,
        ),
        "author": lambda: step_author(
            job=args.job,
            out=args.out,
            provider=args.provider,
            max_iterations=args.max_iterations,
        ),
        "track": lambda: step_track(smoke_add=not args.skip_track_add),
    }

    _p(f"OgunJob pipeline — steps: {' → '.join(steps)}")
    if args.smoke:
        _p("Mode: SMOKE (lexical match; author skipped with 'all')")

    results: List[StepResult] = []
    for name in steps:
        if name == "author" and args.smoke and "all" in args.steps:
            results.append(
                StepResult(
                    name="author",
                    ok=True,
                    summary="skipped in --smoke (use explicit 'author' to test the LLM)",
                )
            )
            _p("\n  [skip] author — use: python pipeline.py author --job ... --out ...")
            continue
        results.append(dispatch[name]())

    return _print_summary(results)


if __name__ == "__main__":
    raise SystemExit(main())
