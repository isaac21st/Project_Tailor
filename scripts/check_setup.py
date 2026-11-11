"""Is this machine ready to run Project Tailor?   python scripts/check_setup.py

Checks your profile files, the PDF browser and fonts (by making a real one-page resume), the local model, and that
the server's port is free. Nothing is sent anywhere. Exit code 0 means everything required works.
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.config import PROJECT_ROOT, load_settings  # noqa: E402

failures = 0


def check(name: str, fn, required: bool = True) -> None:
    global failures
    try:
        detail = fn()
        print(f"[ok]   {name}" + (f": {detail}" if detail else ""))
    except Exception as e:  # noqa: BLE001 - this is a report, not control flow
        tag = "FAIL" if required else "warn"
        print(f"[{tag}] {name}: {e}")
        if required:
            failures += 1


def main() -> int:
    settings = load_settings()
    print(f"Project : {PROJECT_ROOT}")
    print(f"Data    : {settings.data_dir}")
    print(f"Python  : {sys.version.split()[0]}\n")

    def python_version():
        if sys.version_info < (3, 11):
            raise RuntimeError("needs Python 3.11 or newer (3.12 or 3.13 recommended)")

    def base_resume():
        from tailor.resume.schema import Resume

        path = settings.profile_dir / "base_resume.json"
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing: copy your profile bundle into {settings.data_dir}")
        r = Resume.model_validate_json(path.read_text(encoding="utf-8"))
        entries = sum(len(s.entries) for s in r.sections)
        return f"{r.header.name}, {entries} entries"

    def optional_json(name):
        def run():
            path = settings.profile_dir / name
            if not path.exists():
                raise FileNotFoundError("not found (fine if you have not made one)")
            json.loads(path.read_text(encoding="utf-8"))
            return "valid"
        return run

    def fonts():
        found = sorted((PROJECT_ROOT / "tailor" / "resume" / "fonts").glob("Tinos-*.ttf"))
        if len(found) < 4:
            raise RuntimeError(f"expected 4 bundled Tinos fonts, found {len(found)}")
        return f"{len(found)} bundled"

    def real_resume():
        from tailor.resume.auto import default_resume
        from tailor.resume.schema import Resume

        base = Resume.model_validate_json((settings.profile_dir / "base_resume.json").read_text(encoding="utf-8"))
        out = settings.tmp_dir / "check_setup.pdf"
        result = default_resume(base, settings, out)
        rep = result.report
        if not rep.applied:
            raise RuntimeError(rep.reason)
        if not rep.fits:
            raise RuntimeError(f"rendered {rep.pages} pages")
        return f"made a one-page resume ({', '.join(t.split(' (')[0].title() for t, _ in rep.projects[:3])})"

    def ollama():
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as res:
            names = [m["name"] for m in json.load(res).get("models", [])]
        if not names:
            raise RuntimeError("Ollama is running but has no models: run  ollama pull qwen3:8b")
        note = "" if any(n.startswith("qwen3") for n in names) else "  (no qwen3 model yet: ollama pull qwen3:8b)"
        return ", ".join(names) + note

    def port_free():
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", 8765)) == 0:
                raise RuntimeError("port 8765 is already in use (is the server already running?)")
        return "8765 is free"

    def extension():
        m = json.loads((PROJECT_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        return f"{m['name']} v{m['version']}"

    check("Python version", python_version)
    check("Base resume", base_resume)
    check("Standing answers", optional_json("standing_answers.json"), required=False)
    check("Answer sheet", optional_json("answer_sheet.json"), required=False)
    check("Bundled fonts", fonts)
    check("PDF browser + tailoring", real_resume)
    check("Extension files", extension)
    check("Local model (Ollama)", ollama, required=False)
    check("Server port", port_free, required=False)
    print("\n" + ("Everything required works." if not failures else f"{failures} required check(s) failed."))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
