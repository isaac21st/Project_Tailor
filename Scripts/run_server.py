"""Start the local API for the browser extension:  python scripts/run_server.py [--model stub|ollama]

Listens on 127.0.0.1:8765 only. Prints where the access token lives so you can paste it into the extension's
options page once. The model defaults to config.toml (stub if there is none); --model ollama uses local Ollama.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from tailor.config import ModelSettings, load_settings  # noqa: E402
from tailor.fill.profile import profile_from_resume  # noqa: E402
from tailor.llm import make_client  # noqa: E402
from tailor.resume.auto import default_resume  # noqa: E402
from tailor.resume.render import render_resume  # noqa: E402
from tailor.resume.schema import Resume  # noqa: E402
from tailor.server.app import create_app, load_or_create_token  # noqa: E402

PORT = 8765


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", choices=["stub", "ollama", "claude"],
                    help="override the model from config.toml; claude = the Anthropic API (your resume facts and the "
                         "postings are sent to Anthropic; key in ANTHROPIC_API_KEY or <data_dir>/anthropic_key.txt)")
    ap.add_argument("--claude-model", default="claude-sonnet-5-5", help="which Claude model --model claude uses")
    ap.add_argument("--ollama-model", default="qwen3:8b",
                    help="which Ollama model --model ollama uses (default qwen3:8b; qwen3:4b-instruct or qwen3:1.7b need less disk and memory)")
    ap.add_argument("--show-token", action="store_true", help="print the token (default: only its file location)")
    ap.add_argument("--resume-pdf", type=Path, help="the resume PDF the extension uploads (default: output/current_resume.pdf)")
    args = ap.parse_args()

    settings = load_settings()
    settings.ensure_dirs()
    if args.model == "ollama":
        settings.model = ModelSettings(kind="http", base_url="http://localhost:11434/v1", model=args.ollama_model)
    elif args.model == "stub":
        settings.model = ModelSettings(kind="stub")
    elif args.model == "claude":
        settings.model = ModelSettings(kind="anthropic", model=args.claude_model, timeout_s=120.0)

    resume_json = settings.profile_dir / "base_resume.json"
    if not resume_json.exists():
        print(f"Missing {resume_json}. Put your structured resume there first.", file=sys.stderr)
        return 1
    resume = Resume.model_validate_json(resume_json.read_text(encoding="utf-8"))

    extras_file = settings.profile_dir / "profile_overrides.json"
    extra = json.loads(extras_file.read_text(encoding="utf-8")) if extras_file.exists() else {}
    profile = profile_from_resume(resume, overrides=extra.get("name_overrides"), extras=extra.get("extras"),
                                  education_starts=extra.get("education_starts"),
                                  education_order=extra.get("education_order"),
                                  fields_of_study=extra.get("fields_of_study"))

    pdf = args.resume_pdf or settings.output_dir / "current_resume.pdf"
    if args.resume_pdf and not pdf.exists():
        print(f"Resume PDF not found: {pdf}", file=sys.stderr)
        return 1
    if not args.resume_pdf and (not pdf.exists() or pdf.stat().st_mtime < resume_json.stat().st_mtime):
        # the default is an all-round ONE-page version (your full base resume runs longer than a page)
        print("Building your all-round one-page resume (used on pages that have no job posting)...")
        made = default_resume(resume, settings, pdf)
        if not made.report.applied or not made.report.fits:
            print("  could not build it; falling back to the plain base resume.", file=sys.stderr)
            render_resume(resume, pdf, settings)

    token = load_or_create_token(settings)
    print(f"\nTailor Assist server  http://127.0.0.1:{PORT}   model: {settings.model.kind}"
          + (f" ({settings.model.model})" if settings.model.kind == "http" else ""))
    print(f"Resume the extension uploads when a page has no job posting: {pdf}")
    print("On pages with a posting it tailors your resume to that posting first (rules only, checked against your base resume).")
    print(f"Access token file: {settings.data_dir / 'server_token.txt'}")
    if args.show_token:
        print(f"Token: {token}")
    print("Paste the token into the extension's Options page once. Ctrl+C stops the server.\n")

    app = create_app(settings, make_client(settings.model, settings.data_dir), profile, token, pdf, base_resume=resume)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
