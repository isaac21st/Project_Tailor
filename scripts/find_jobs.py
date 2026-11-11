"""Search companies' public job feeds for roles that fit, from the command line (the Tailor Desk's Find roles tab does
the same with buttons): python scripts/find_jobs.py [--top 20]

Uses the preferences saved on the Desk (<data_dir>/search/prefs.json) and stores results where the Desk shows them.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.config import load_settings  # noqa: E402
from tailor.jobs.search import SearchStore, company_name, grouped, run_search, spread  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    settings = load_settings()
    store = SearchStore(settings.data_dir)
    resume = (settings.profile_dir / "base_resume.json").read_text(encoding="utf-8")
    s = run_search(store.prefs(), store, resume, lambda stage, d, t: print(f"\r{stage}: {d}/{t}   ", end="", flush=True))
    print(f"\nRead {s['read']} openings from {s['feeds']} companies: {s['kept']} fit, {s['new']} new.")
    rows = spread([r for r in grouped(list(store.load()["results"].values())) if not r.get("dismissed")])
    for i, r in enumerate(rows[: args.top], 1):
        print(f"{i:2}. [{r['fit']:3}] {company_name(r)}: {r['title']} ({r['location'][:40]})\n     {r['url']}")
    print("\nSave the ones you like from the Desk: http://127.0.0.1:8765/desk (Find roles).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
