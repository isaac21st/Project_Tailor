"""Render a resume JSON file to PDF:  python scripts/render_resume.py resume.json out.pdf"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.resume.render import render_resume  # noqa: E402
from tailor.resume.schema import Resume  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("resume_json", type=Path)
    ap.add_argument("out_pdf", type=Path)
    args = ap.parse_args()

    resume = Resume.model_validate_json(args.resume_json.read_text(encoding="utf-8"))
    report = render_resume(resume, args.out_pdf)
    print(f"wrote {report.pdf_path}")
    print(f"pages={report.pages} fits={report.fits} spare={report.spare_px:.1f}px "
          f"of {report.available_height_px:.0f}px  grid_error={report.line_grid_error:.3f}")
    return 0 if report.fits else 1


if __name__ == "__main__":
    raise SystemExit(main())
