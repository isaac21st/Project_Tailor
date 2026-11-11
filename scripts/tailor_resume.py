"""Tailor your resume to a job posting, on your machine.

  python scripts/tailor_resume.py --job posting.pdf --out tailored.pdf
  python scripts/tailor_resume.py --job posting.raw.txt --title "Associate Product Manager" --out tailored.pdf --json report.json
  python scripts/tailor_resume.py --job posting.pdf --hard            # the local model re-words bullets in the posting's language
  python scripts/tailor_resume.py --job posting.pdf --hard --ask "lean on the AI/ML qualification"

It chooses and orders what your resume already says (projects, skills, MS or MBA first), trims the least relevant
lines until it fits one page, and refuses to return anything that adds a claim. The report says what it did.
With --hard it also re-words the shown bullets in the posting's language (Ollama must be running); every re-worded
bullet is checked against the one it replaces, and refused re-wordings are listed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.config import ModelSettings, load_settings  # noqa: E402
from tailor.jobs import load_job_text  # noqa: E402
from tailor.llm import make_client  # noqa: E402
from tailor.resume.auto import tailor_resume  # noqa: E402
from tailor.resume.schema import Resume  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", required=True, type=Path, help="the posting: a PDF, or a text file")
    ap.add_argument("--title", default="", help="the job title, if the posting text does not start with it")
    ap.add_argument("--out", type=Path, help="where to write the tailored PDF (default: output/tailored/<id>.pdf)")
    ap.add_argument("--json", type=Path, help="also write the report as JSON")
    ap.add_argument("--hard", action="store_true", help="re-word bullets in the posting's language with the local model")
    ap.add_argument("--ask", default="", help="with --hard: what to lean on, e.g. 'lead with the product work'")
    ap.add_argument("--ollama-model", default="qwen3:8b", help="with --hard: which Ollama model (default qwen3:8b)")
    args = ap.parse_args()

    settings = load_settings()
    base = Resume.model_validate_json((settings.profile_dir / "base_resume.json").read_text(encoding="utf-8"))
    job_text = load_job_text(args.job) if args.job.suffix.lower() == ".pdf" else args.job.read_text(encoding="utf-8")

    out = args.out
    if out is None:
        out = settings.output_dir / "tailored" / f"{args.job.stem}.pdf"
    client = make_client(ModelSettings(kind="http", model=args.ollama_model)) if args.hard else None
    result = tailor_resume(base, job_text, title=args.title, settings=settings, out_pdf=out, client=client,
                           instruction=args.ask)
    r = result.report
    print(r.summary())
    if not r.applied:
        return 2
    print(f"  education first : {r.education_first}  ({r.education_reason})")
    print("  projects        : " + "; ".join(f"{t} [{s}]" for t, s in r.projects))
    if r.dropped_projects:
        print("  left out        : " + "; ".join(f"{t} [{s}]" for t, s in r.dropped_projects))
    print("  skills order    : " + ", ".join(r.skills_order))
    for line in r.trimmed:
        print("  trimmed         : " + line)
    for line in r.rewrites:
        print("  reworded        : " + line)
    for line in r.refused:
        print("  not reworded    : " + line)
    for line in r.gaps + r.warnings:
        print("  note            : " + line)
    print(f"  page            : {r.pages} page(s), spare {r.spare_px:.0f}px, fits={r.fits}")
    print(f"  saved           : {out}")
    if args.json:
        args.json.write_text(json.dumps(r.as_dict(), indent=2), encoding="utf-8")
    return 0 if r.fits else 1


if __name__ == "__main__":
    raise SystemExit(main())
