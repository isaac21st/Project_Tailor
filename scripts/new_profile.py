"""Start your own profile from the examples:   python scripts/new_profile.py
                     or from your resume:      python scripts/new_profile.py --resume my_resume.pdf     (PDF, Word or text)

Copies examples/profile/* into <data_dir>/profile/ (default ~/project_tailor_data/profile), skipping any file you
already have, so it never overwrites your own. Then edit those copies: base_resume.json is your resume, the others
are your preferences (see PREFERENCES.md). Your data folder stays on your computer; it is not part of the project.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailor.config import load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--resume", type=Path, help="read this resume into base_resume.json, in place of the example person")
    args = ap.parse_args()
    settings = load_settings()
    settings.ensure_dirs()
    src = ROOT / "examples" / "profile"
    dest = settings.profile_dir
    dest.mkdir(parents=True, exist_ok=True)
    if args.resume and not (dest / "base_resume.json").exists():
        from tailor.profile.import_resume import import_resume
        got = import_resume(args.resume)
        (dest / "base_resume.json").write_text(got.resume.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        print(f"read your resume into {dest / 'base_resume.json'}. Every tailored version is checked against it, so read it once.")
        for w in got.warnings:
            print(f"  check: {w}")
    for f in sorted(src.glob("*.json")):
        target = dest / f.name
        if target.exists():
            print(f"kept yours  {target}")
            continue
        shutil.copy2(f, target)
        print(f"copied      {target}")
    print(f"\nNext: replace the example person in {dest / 'base_resume.json'} with your own resume, then run\n"
          f"  python scripts/check_setup.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
