#!/usr/bin/env python3
"""
build_profile.py
-----------------
Read your CV, public GitHub repositories, and (optionally) extra documents
(papers, project notes), then call the extraction agent to produce a
profile DRAFT (config/master_profile_draft.yaml).

This draft is NEVER used directly by tailor.py: open it, review it
(especially blocks with needs_review: true), and copy validated blocks
by hand into config/cv_master.yaml.

After human review, blocks with needs_review: false can also be written
to memory/profile.md, but ONLY with --commit-to-memory: that step never
starts on its own.

Usage:
    python build_profile.py --cv path/to/your_cv.pdf --github your-username \\
        --docs sources/paper1.pdf sources/project_notes.md

    # see what would be sent to the model, without spending API calls:
    python build_profile.py --cv cv.pdf --github your-username --dry-run

    python build_profile.py --provider lmstudio --cv cv.pdf --github your-username
    python build_profile.py --provider deepseek --cv cv.pdf --github your-username

    # after reviewing the YAML draft (never automatic):
    python build_profile.py --commit-to-memory
    python build_profile.py --commit-to-memory --dry-run
"""

import argparse
from pathlib import Path

import yaml
from dotenv import load_dotenv

from ingest.extract import commit_validated_to_memory, extract_profile
from ingest.sources import DB_PATH, load_db_skills, load_local_docs, load_text_file, fetch_github_repos
from llm_provider import SUPPORTED_PROVIDERS

load_dotenv(Path(__file__).resolve().parent / ".env")

DEFAULT_DRAFT = "config/master_profile_draft.yaml"
DEFAULT_MEMORY = "memory/profile.md"


def render_draft_yaml(data: dict) -> str:
    header = (
        "# =========================================================\n"
        "# DRAFT generated automatically by build_profile.py\n"
        "#\n"
        "# Every block has needs_review + review_note: check those with\n"
        "# needs_review: true BEFORE copying them into config/cv_master.yaml.\n"
        "# Nothing in this file is used directly by tailor.py.\n"
        "# To copy validated blocks into memory/profile.md (after human\n"
        "# review): python build_profile.py --commit-to-memory\n"
        "# =========================================================\n\n"
    )
    return header + yaml.dump(data, allow_unicode=True, sort_keys=False)


def _load_draft(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"Draft not found: {path}\n"
            "Generate it first with build_profile.py --cv ..., review it, "
            "then rerun with --commit-to-memory."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid draft (expected a YAML object): {path}")
    return data


def _run_commit_to_memory(args) -> None:
    draft_path = Path(args.out)
    dest = Path(args.memory_out)
    data = _load_draft(draft_path)

    try:
        markdown, kept, skipped = commit_validated_to_memory(
            data,
            dest,
            source_label=str(draft_path).replace("\\", "/"),
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        raise SystemExit(str(e)) from e

    if args.dry_run:
        print("=== DRY RUN --commit-to-memory: no write to disk ===\n")
        print(f"Draft: {draft_path}")
        print(f"Destination: {dest}")
        print(f"Validated blocks: {kept}  |  skipped (needs_review): {skipped}\n")
        print(markdown)
        return

    print(f"Wrote {kept} validated blocks to: {dest}")
    if skipped:
        print(
            f"Skipped {skipped} blocks with needs_review: true "
            f"(they remain in {draft_path})."
        )
    print("config/cv_master.yaml was not modified.")


def main():
    parser = argparse.ArgumentParser(
        description="Build a profile draft from CV + GitHub + extra documents"
    )
    parser.add_argument(
        "--cv",
        help="Path to the CV file (pdf, docx, or txt). Required unless --commit-to-memory.",
    )
    parser.add_argument("--github", help="GitHub username whose public repos should be read")
    parser.add_argument(
        "--docs", nargs="*", default=[], help="Paths to extra documents (papers, project notes)"
    )
    parser.add_argument("--out", default=DEFAULT_DRAFT)
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not call the API; show what would be sent"
    )
    parser.add_argument(
        "--provider",
        choices=list(SUPPORTED_PROVIDERS),
        default=None,
        help="LLM provider (overrides LLM_PROVIDER in .env). Default: anthropic",
    )
    parser.add_argument(
        "--commit-to-memory",
        action="store_true",
        help=(
            "After human review: copy blocks with needs_review: false "
            "from --out (default: config/master_profile_draft.yaml) into "
            "memory/profile.md. Does not extract and never runs automatically."
        ),
    )
    parser.add_argument(
        "--memory-out",
        default=DEFAULT_MEMORY,
        help="Destination path for --commit-to-memory (default: memory/profile.md)",
    )
    parser.add_argument(
        "--no-db-skills",
        action="store_true",
        help="Do not read the skills table in ogunjob.db as extra context",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"Path to ogunjob.db (default: {DB_PATH})",
    )
    args = parser.parse_args()

    if args.commit_to_memory:
        extracting = bool(args.cv or args.github or args.docs)
        if extracting:
            parser.error(
                "--commit-to-memory does not extract: review the YAML draft first, "
                "then run only --commit-to-memory (without --cv/--github/--docs)."
            )
        _run_commit_to_memory(args)
        return

    if not args.cv:
        parser.error("--cv is required, unless --commit-to-memory")

    print(f"Reading CV from {args.cv}...")
    cv_text = load_text_file(args.cv)

    repos = []
    if args.github:
        print(f"Fetching public repositories for {args.github}...")
        try:
            repos = fetch_github_repos(args.github)
            print(f"  found {len(repos)} repos (forks excluded)")
        except Exception as e:
            print(f"  WARNING: could not read GitHub repos ({e}), continuing without them.")

    extra_docs = load_local_docs(args.docs) if args.docs else []

    db_skills = []
    if args.no_db_skills:
        print("DB context: skipped (--no-db-skills).")
    else:
        db_path = Path(args.db) if args.db else DB_PATH
        db_skills = load_db_skills(db_path)
        if not db_path.exists():
            print(f"DB context: {db_path} missing, continuing without extra skills.")
        elif not db_skills:
            print(f"DB context: no skills in {db_path} (table missing or empty).")
        else:
            print(f"DB context: {len(db_skills)} skills from {db_path} (read-only).")

    print("Dry run: no API call..." if args.dry_run else "Extracting profile...")
    data = extract_profile(
        cv_text,
        repos,
        extra_docs,
        dry_run=args.dry_run,
        provider=args.provider,
        db_skills=db_skills,
    )

    if args.dry_run:
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_draft_yaml(data), encoding="utf-8")

    n_total = len(data.get("experiences", [])) + len(data.get("projects", []))
    n_review = sum(
        1 for e in data.get("experiences", []) + data.get("projects", []) if e.get("needs_review")
    )

    print(f"\nDraft written to: {out_path}")
    if data.get("gaps"):
        print("\n⚠️  General notes on the profile:")
        for g in data["gaps"]:
            print(f"  - {g}")

    print(f"\n{n_review} of {n_total} blocks flagged for manual review.")
    print("Next step: open the draft, fix or drop what you need, and")
    print("copy the validated blocks into config/cv_master.yaml.")
    print("Only after review, if you also want to update memory/profile.md:")
    print("  python build_profile.py --commit-to-memory")


if __name__ == "__main__":
    main()
