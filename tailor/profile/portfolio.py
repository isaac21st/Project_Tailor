"""Crawl the owner's own published portfolio into plain text, one file per page.

The site is server-rendered (Framer), so plain HTTP is enough. Pages carry CSS text and duplicated
navigation (desktop and mobile variants); this keeps only the readable content, in reading order.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html

USER_AGENT = "project_tailor/0.1 (owner reading their own portfolio)"
_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template"}
_NAV_WORDS = {"Work", "About", "Design/Photo", "Contact", "Home"}
_SITE_LINK = re.compile(r"^(?:\./)?(?:work/[^/?#]+|about|design-photos)/?$")


@dataclass
class Page:
    url: str
    slug: str
    title: str
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def extract_lines(page_html: str) -> tuple[str, list[str]]:
    """(title, readable lines in order). Drops CSS/JS, navigation words and repeated lines."""
    doc = html.fromstring(page_html)
    title = " ".join((doc.findtext(".//title") or "").split())
    raw: list[str] = []

    def walk(el) -> None:
        if not isinstance(el.tag, str) or el.tag in _SKIP_TAGS:
            if el.tail:
                raw.append(el.tail)
            return
        if el.text:
            raw.append(el.text)
        for child in el:
            walk(child)
        if el.tail:
            raw.append(el.tail)

    body = doc.body if doc.body is not None else doc
    walk(body)

    lines: list[str] = []
    recent: list[str] = []
    for r in _join_letter_runs(raw):
        s = " ".join(r.split())
        if not s or s in _NAV_WORDS or s.startswith((":root", "html body")):
            continue
        if len(s) > 3 and s in recent:  # Framer repeats blocks for desktop/mobile layouts
            continue
        lines.append(s)
        recent = (recent + [s])[-8:]
    return title, lines


def _join_letter_runs(raw: list[str]) -> list[str]:
    """Typewriter headlines are one element per letter. Glue such runs back into one string, keeping
    the space elements, so the text reads as written (and repeated letters are never dropped)."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if sum(1 for x in run if x.strip()) >= 5:
            out.append("".join(run))
        else:
            out.extend(x for x in run if x.strip())
        run.clear()

    for r in raw:
        if len(r.strip()) <= 1 and len(r) <= 2:
            run.append(r)
        else:
            flush()
            out.append(r)
    flush()
    return out


def _discover(index_html: str, base: str) -> list[str]:
    doc = html.fromstring(index_html)
    found: dict[str, None] = {}
    for a in doc.iter("a"):
        href = (a.get("href") or "").strip()
        if _SITE_LINK.match(href) and not href.rstrip("/").endswith("/contact"):
            found[urljoin(base, href)] = None
    return list(found)


def crawl_portfolio(base_url: str, out_dir: Path, delay_s: float = 0.6) -> list[Page]:
    base = base_url.rstrip("/") + "/"
    host = urlparse(base).netloc
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Page] = []
    with httpx.Client(follow_redirects=True, timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        seeds = [base, urljoin(base, "work")]
        queue: list[str] = []
        for seed in seeds:
            r = client.get(seed)
            r.raise_for_status()
            for u in _discover(r.text, seed):
                if urlparse(u).netloc == host and u not in queue:
                    queue.append(u)
        for url in queue:
            time.sleep(delay_s)
            r = client.get(url)
            if r.status_code != 200:
                continue
            title, lines = extract_lines(r.text)
            slug = urlparse(url).path.strip("/").replace("/", "__") or "home"
            page = Page(url=url, slug=slug, title=title, lines=lines)
            (out_dir / f"{slug}.txt").write_text(f"URL: {url}\nTITLE: {title}\n\n{page.text}\n", encoding="utf-8")
            pages.append(page)
    return pages
