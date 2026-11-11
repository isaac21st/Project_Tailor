"""Run the tailoring API on this computer:  python scripts/run_api.py        (docs at http://127.0.0.1:8770/docs)

    python scripts/run_api.py --new-key     prints a new key and the hash to put in TAILOR_API_KEYS
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from tailor.api import create_api, new_key  # noqa: E402
from tailor.config import apply_environment, load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--new-key", action="store_true")
    args = ap.parse_args()
    if args.new_key:
        key, hashed = new_key()
        print(f"Key (shown once, give it to the caller): {key}\nFor TAILOR_API_KEYS: {hashed}")
        return 0
    apply_environment(load_settings())  # where the PDF browser lives
    uvicorn.run(create_api(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
