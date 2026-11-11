"""Checks on model output about a job. A model may only quote the posting, never invent from it."""
from __future__ import annotations

import re
import unicodedata


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[‐-―]", "-", s)  # every dash style -> "-"
    return re.sub(r"\s+", " ", s).strip()


def missing_keywords(keywords: list[str], posting_text: str) -> list[str]:
    """Keywords that do NOT appear verbatim (ignoring case, dash style and spacing) in the posting."""
    haystack = _norm(posting_text)
    return [k for k in keywords if _norm(k) not in haystack]
