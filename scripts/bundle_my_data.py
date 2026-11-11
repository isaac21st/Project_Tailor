"""Pack your own Project Tailor data to move it to another computer:   python scripts/bundle_my_data.py [zip path]

Takes your data folder (default ~/project_tailor_data): profile, Desk, role search, network, tailored resumes and letters.
Leaves out what each computer makes for itself (the Python environment, browsers, logs, temporary files) and the server's
token (the new computer gets its own). Then, on Windows:
    powershell -ExecutionPolicy Bypass -File scripts\\setup_windows.ps1 -Bundle <this zip>
The zip holds your resume, answers and contacts: move it yourself (USB stick, AirDrop, your own cloud drive) and delete it
afterwards. It is never part of the project and never goes to GitHub.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailor.config import load_settings  # noqa: E402

SKIP_DIRS = {"venv", "browsers", "logs", "tmp", "benchmarks", "trial_browser", "watch_browser", "trials", "__pycache__"}
SKIP_FILES = {"server_token.txt", ".DS_Store"}


def main() -> int:
    data = load_settings().data_dir
    out = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / "Desktop" / "project_tailor_data_bundle.zip"
    if not (data / "profile" / "base_resume.json").exists():
        print(f"No profile in {data}: nothing to bundle.")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(data.rglob("*")):
            rel = path.relative_to(data)
            if path.is_dir() or rel.parts[0] in SKIP_DIRS or path.name in SKIP_FILES or "__pycache__" in rel.parts:
                continue
            z.write(path, rel.as_posix())
            n += 1
    print(f"Packed {n} files ({out.stat().st_size / 1e6:.1f} MB) into {out}")
    print("It holds your personal data: move it yourself, then delete it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
