"""Save a readable copy of what forms get filled with (work history, education, My info) in the project folder.

    python scripts/save_my_info.py            # writes My_Application_Info.json next to README.md

The server still reads ~/project_tailor_data (a login item cannot read OneDrive); this is the owner's copy, kept
current by `bash scripts/autostart.sh update`. Self-ID and other sensitive answers are not included.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailor.fill.mapper import _place  # noqa: E402
from tailor.fill.profile import _fmt, profile_from_resume  # noqa: E402
from tailor.resume.schema import Resume  # noqa: E402


def main() -> int:
    data = Path(os.environ.get("TAILOR_DATA", Path.home() / "project_tailor_data")) / "profile"
    overrides = json.loads((data / "profile_overrides.json").read_text(encoding="utf-8"))
    resume = Resume.model_validate_json((data / "base_resume.json").read_text(encoding="utf-8"))
    p = profile_from_resume(resume, overrides=overrides.get("name_overrides"), extras=overrides.get("extras"),
                            education_starts=overrides.get("education_starts"),
                            education_order=overrides.get("education_order"),
                            fields_of_study=overrides.get("fields_of_study"))
    out = {
        "note": "Copy of what Project Tailor fills into applications. The live data is ~/project_tailor_data/profile.",
        "my_info": overrides.get("extras", {}),
        "work_history": [
            {"employer": e.employer, "title": e.title, "city": _place(e.location)[0], "state": _place(e.location)[1],
             "start": _fmt(e.start), "end": "present" if e.current else _fmt(e.end)}
            for e in p.experience if e.kind == "job"],
        "education": [
            {"school": e.school, "degree": e.degree, "field": e.field, "field_on_forms": e.form_field,
             "status": "Now attending" if e.expected else "Graduated",
             "start": _fmt(e.start) if e.start else None, "end": _fmt(e.end)}
            for e in p.education],
        "profile_overrides": overrides,
    }
    path = ROOT / "My_Application_Info.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
