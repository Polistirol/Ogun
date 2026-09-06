"""
loop.py
-------
Evaluator-optimizer orchestrator: Author writes, Evaluator scores,
if the draft misses the threshold it goes back for a targeted revision.
At most N iterations (default 3), then it stops anyway: the user always
reviews the draft, passed or not.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

from llm_provider import SUPPORTED_PROVIDERS

from .author import generate_draft, load_cv_master, render_markdown_cv, revise_draft
from .evaluator import PASS_THRESHOLD, evaluate

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEFAULT_MAX_ITERATIONS = 3


@dataclass
class LoopResult:
    draft: dict
    evaluations: List[dict] = field(default_factory=list)
    iterations: int = 0
    passed: bool = False
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    threshold: int = PASS_THRESHOLD

    @property
    def last_evaluation(self) -> Optional[dict]:
        return self.evaluations[-1] if self.evaluations else None


def _issues_for_revision(evaluation: dict) -> List[str]:
    issues = list(evaluation.get("issues") or [])
    if issues:
        return issues
    score = evaluation.get("score", 0)
    return [
        f"Score {score} below threshold {PASS_THRESHOLD}: improve coverage "
        "of skills present in CV_MASTER, factual consistency and clarity, "
        "without inventing facts."
    ]


def run_loop(
    cv_master: dict,
    job_text: str,
    provider: Optional[str] = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> LoopResult:
    if max_iterations < 1:
        raise ValueError("max_iterations deve essere >= 1")

    result = LoopResult(draft={}, max_iterations=max_iterations)
    draft: Optional[dict] = None

    for round_n in range(1, max_iterations + 1):
        print(f"\n--- Round {round_n}/{max_iterations} ---")
        if draft is None:
            print("Author: first draft...")
            draft = generate_draft(cv_master, job_text, provider=provider)
        else:
            print("Author: targeted revision on the issues...")
            draft = revise_draft(
                cv_master,
                job_text,
                draft,
                _issues_for_revision(result.evaluations[-1]),
                provider=provider,
            )

        print("Evaluator: scoring...")
        evaluation = evaluate(cv_master, job_text, draft, provider=provider)
        evaluation_record = {
            "round": round_n,
            "score": evaluation["score"],
            "issues": evaluation["issues"],
            "passed": evaluation["passed"],
        }
        result.draft = draft
        result.evaluations.append(evaluation_record)
        result.iterations = round_n
        result.passed = evaluation["passed"]

        print(f"Score: {evaluation['score']}/100  passed={evaluation['passed']}")
        if evaluation["issues"]:
            print("Issues:")
            for issue in evaluation["issues"]:
                print(f"  - {issue}")
        else:
            print("Issues: none")

        if evaluation["passed"]:
            print(
                f"Threshold {PASS_THRESHOLD} passed at round {round_n}. "
                "This is still a draft — re-read it before sending."
            )
            return result

    print(
        f"\nThe draft did NOT pass threshold {PASS_THRESHOLD} after "
        f"{max_iterations} rounds. Review it yourself: do not send it as-is."
    )
    return result


def _write_outputs(out_dir: Path, loop_result: LoopResult, candidate: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = loop_result.draft
    (out_dir / "cv_tailored.md").write_text(
        render_markdown_cv(data, candidate), encoding="utf-8"
    )
    (out_dir / "cover_letter.md").write_text(
        data.get("cover_letter", ""), encoding="utf-8"
    )
    (out_dir / "raw_output.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "evaluation.json").write_text(
        json.dumps(
            {
                "passed": loop_result.passed,
                "iterations": loop_result.iterations,
                "max_iterations": loop_result.max_iterations,
                "threshold": loop_result.threshold,
                "evaluations": loop_result.evaluations,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate a tailored CV and cover letter for a job ad "
        "(Author/Evaluator loop, draft to review)"
    )
    parser.add_argument("--job", type=str, help="Path to a text file with the job ad")
    parser.add_argument(
        "--job-text", type=str, help="Job-ad text pasted directly"
    )
    parser.add_argument("--cv-master", type=str, default="config/cv_master.yaml")
    parser.add_argument("--out", type=str, required=True, help="Output directory")
    parser.add_argument(
        "--provider",
        choices=list(SUPPORTED_PROVIDERS),
        default=None,
        help="LLM provider (overrides LLM_PROVIDER in .env). Default: anthropic",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help=f"Max Author/Evaluator rounds (default {DEFAULT_MAX_ITERATIONS})",
    )
    args = parser.parse_args(argv)

    if not args.job and not args.job_text:
        print("Need --job <file> or --job-text <text>")
        sys.exit(1)
    if args.max_iterations < 1:
        print("--max-iterations must be >= 1")
        sys.exit(1)

    job_text = args.job_text or Path(args.job).read_text(encoding="utf-8")
    cv_master = load_cv_master(args.cv_master)

    print("Generating CV and cover letter (Author/Evaluator loop)...")
    try:
        loop_result = run_loop(
            cv_master,
            job_text,
            provider=args.provider,
            max_iterations=args.max_iterations,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        sys.exit(1)

    out_dir = Path(args.out)
    _write_outputs(out_dir, loop_result, cv_master["candidate"])

    print(f"\nDone. Files written to: {out_dir}/")
    print(" - cv_tailored.md")
    print(" - cover_letter.md")
    print(" - raw_output.json (raw data, useful for debug)")
    print(" - evaluation.json (score/issues per round)")

    if loop_result.draft.get("warnings"):
        print("\nWARNING, the model flagged:")
        for warning in loop_result.draft["warnings"]:
            print(f"  - {warning}")

    status = "threshold passed" if loop_result.passed else "threshold NOT passed"
    print(
        f"\nLoop: {loop_result.iterations} rounds, {status} "
        f"(threshold {loop_result.threshold})."
    )
    print(
        f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        "RE-READ BEFORE SENDING. This is a draft, not an automatic submission."
    )


if __name__ == "__main__":
    main()
