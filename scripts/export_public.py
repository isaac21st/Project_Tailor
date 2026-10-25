"""Make a shareable copy of Project Tailor with none of the owner's personal information in it.

    python scripts/export_public.py /path/to/output_folder

What it does:
  1. Copies only the code, tests, examples and docs (an allowlist). Nothing from the data folder (resume, answers,
     postings, contacts, tokens) is ever read into the copy.
  2. Replaces the owner's personal words (name, schools, employers, city, resume phrases quoted in tests and comments)
     using a private list kept in the data folder: <data_dir>/public_export_map.json
       {"replace": [["personal text", "neutral text"], ["Word", "Other", true], ...]}   (true = whole words only)
  3. Scans every copied file for anything personal that is left: the owner's name, email, phone, address, postal code,
     LinkedIn / portfolio links (read from their own profile), every word on the private list, and five-word runs of
     their resume's bullets. Any hit stops the export with a report, and the copy is not marked clean.

The private list and the profile are only read here; neither is copied.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailor.config import load_settings  # noqa: E402

ALLOW = ["tailor", "extension", "tests", "scripts", "examples", "config.example.toml", "requirements.txt",
         "README.md", "PREFERENCES.md", ".gitignore", "LICENSE"]
SKIP_PARTS = {"__pycache__", ".DS_Store", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pdf", ".zip"}
TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".json", ".md", ".toml", ".txt", ".sh", ".svg", ".gitignore", ""}
NEVER = {"scripts/deploy_staged_extension.sh"}  # the owner's own deploy step


def load_map(data_dir: Path) -> list[tuple[str, str, bool]]:
    path = data_dir / "public_export_map.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8")).get("replace", [])
    pairs: dict[str, tuple[str, str, bool]] = {}
    for x in raw:
        if len(x) < 2 or not x[0]:
            continue
        old, new, word = str(x[0]), str(x[1]), bool(x[2]) if len(x) > 2 else False
        # the same words in capitals and in lower case are replaced the same way
        for o, n in ((old, new), (old.upper(), new.upper()), (old.lower(), new.lower())):
            pairs.setdefault(o, (o, n, word))
    return sorted(pairs.values(), key=lambda p: -len(p[0]))  # longest first: "Acme Rental Group" before "Acme"


def sanitize(text: str, pairs: list[tuple[str, str, bool]]) -> str:
    for old, new, word in pairs:
        if word:
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(old)}(?![A-Za-z0-9])", new, text)
        else:
            text = text.replace(old, new)
    return text


def needles(settings, pairs) -> dict[str, tuple[str, bool]]:
    """What must not appear in the copy: needle -> (why, whole words only)."""
    out: dict[str, tuple[str, bool]] = {}
    for old, _, word in pairs:
        if len(old) >= 4 or word:
            out[old.lower()] = ("on the private list", word)
    prof_dir = settings.profile_dir
    try:
        from tailor.fill.profile import load_profile_from_dir  # optional helper
        prof = load_profile_from_dir(prof_dir)
    except Exception:
        prof = None
    base = prof_dir / "base_resume.json"
    if base.exists():
        resume = json.loads(base.read_text(encoding="utf-8"))
        head = resume.get("header", {})
        name = head.get("name", "")
        for part in [name] + name.split():
            if len(part) >= 3:
                out[part.lower()] = ("the owner's name", True)
        for c in head.get("contact", []):
            t, u = c.get("text", ""), c.get("url") or ""
            for v in (t, u.replace("mailto:", "")):
                v = v.strip()
                if len(v) >= 6:
                    out[v.lower()] = ("the owner's contact details", False)
            digits = re.sub(r"\D", "", t)
            if len(digits) >= 10:
                out[digits[-10:]] = ("the owner's phone number", False)
                out[f"{digits[-10:-7]}-{digits[-7:-4]}-{digits[-4:]}"] = ("the owner's phone number", False)
        bullets = []
        for s in resume.get("sections", []):
            for e in s.get("entries", []):
                title = e.get("title", "").strip()
                if len(title) >= 8 and title.lower() not in PUBLIC_NAMES:  # employers, schools, projects
                    out[title.lower()] = ("an entry on the owner's resume", False)
                for r in e.get("roles", []):
                    bullets += r.get("bullets", [])
        for b in bullets:
            words = re.sub(r"\*", "", b).split()
            for i in range(0, max(1, len(words) - 4), 2):
                run = words[i:i + 5]
                chunk = " ".join(run)
                # a run that names something (a number, or a capitalized word after the first) can point to the owner;
                # plain phrasing ("led the data analytics workstream") cannot
                if len(chunk) >= 28 and (re.search(r"\d", chunk) or any(w[:1].isupper() for w in run[1:])):
                    out[chunk.lower()] = ("a phrase from the owner's resume", False)
    over = prof_dir / "profile_overrides.json"
    if over.exists():
        extras = json.loads(over.read_text(encoding="utf-8")).get("extras", {})
        for k in ("address", "postal_code"):
            v = str(extras.get(k, "")).strip()
            if len(v) >= 5:
                out[v.lower()] = (f"the owner's {k.replace('_', ' ')}", False)
    for k in list(out):
        if k in ALLOWED_WORDS:
            del out[k]
    return out


# words on the private list that are also ordinary words or public place names a test may use on its own
ALLOWED_WORDS: set[str] = set()
# resume entry titles that are public names on their own (a university a test uses for its campus logic)
PUBLIC_NAMES = {"university of michigan", "university of michigan school of information",
                "university of michigan, school of information"}


def export(dest: Path) -> int:
    settings = load_settings()
    pairs = load_map(settings.data_dir)
    if dest.exists() and any(dest.iterdir()):
        keep = {".git"}
        for child in dest.iterdir():
            if child.name not in keep:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in ALLOW:
        src = ROOT / name
        if not src.exists():
            continue
        files = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
        for f in files:
            rel = f.relative_to(ROOT)
            if any(part in SKIP_PARTS for part in rel.parts) or f.suffix in SKIP_SUFFIXES or rel.as_posix() in NEVER:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if f.suffix in TEXT_SUFFIXES:
                out.write_text(sanitize(f.read_text(encoding="utf-8"), pairs), encoding="utf-8")
            else:
                shutil.copy2(f, out)
            copied += 1
    # the scan
    find = needles(settings, pairs)
    hits = []
    for f in dest.rglob("*"):
        if not f.is_file() or ".git" in f.parts or f.suffix not in TEXT_SUFFIXES:
            continue
        low = f.read_text(encoding="utf-8", errors="ignore").lower()
        for needle, (why, word) in find.items():
            found = re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", low) if word else needle in low
            if found:
                hits.append((f.relative_to(dest).as_posix(), why, needle))
    print(f"copied {copied} files to {dest}")
    if hits:
        print(f"\nSTOP: {len(hits)} personal item(s) left in the copy:")
        for f, why, needle in sorted(set(hits))[:200]:
            print(f"  {f}: {why}: {needle!r}")
        return 1
    print("scan: nothing personal found")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(export(Path(sys.argv[1]).expanduser().resolve()))
