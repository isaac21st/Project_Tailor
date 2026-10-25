"""Start your own profile from the examples:   python scripts/new_profile.py

Copies examples/profile/* into <data_dir>/profile/ (default ~/project_tailor_data/profile), skipping any file you
already have, so it never overwrites your own. Then edit those copies: base_resume.json is your resume, the others
are your preferences (see PREFERENCES.md). Your data folder stays on your computer; it is not part of the project.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailor.config import load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    settings.ensure_dirs()
    src = ROOT / "examples" / "profile"
    dest = settings.profile_dir
    dest.mkdir(parents=True, exist_ok=True)
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
