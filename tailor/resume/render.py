"""Render a Resume straight to PDF from a fixed HTML/CSS template (no Word step).

Why: Word reflows text every time it lays a page out, then a converter reflows it
again, which is what warps bullets and stretches spacing. Here every spacing value
is a constant in the template, so wording only changes the blank space left at the
bottom of the page. The render also reports whether it fits on one page and whether
every entry sits on the fixed line grid, so callers can shorten text instead of
squeezing spacing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from pypdf import PdfReader

from ..config import Settings, apply_environment, load_settings
from .schema import Resume

HERE = Path(__file__).resolve().parent
FONTS_DIR = HERE / "fonts"
MARGINS = {"top": 0.4, "right": 0.5, "bottom": 0.3, "left": 0.5}  # inches, same as the Word version
PAGE_HEIGHT_IN = 11.0
PX_PER_IN = 96
_FONT_FACES = ("10.5pt Tinos", "bold 10.5pt Tinos", "italic 10.5pt Tinos", "bold italic 10.5pt Tinos")

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"\*(.+?)\*")

_MEASURE_JS = """
() => {
  const page = document.querySelector('.page');
  const entries = [...document.querySelectorAll('.entry')].map(e => {
    const head = e.querySelector('.entry-head');
    const cs = getComputedStyle(e);
    const lh = parseFloat(cs.lineHeight);
    const pad = parseFloat(getComputedStyle(head).paddingTop);
    const lines = (e.getBoundingClientRect().height - pad) / lh;
    return { lines: lines };
  });
  return { height: page.getBoundingClientRect().height, entries: entries };
}
"""


def inline(text: str) -> Markup:
    """Escape text, then turn **bold** and *italic* marks into tags."""
    s = str(escape(text))
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _ITALIC.sub(r"<em>\1</em>", s)
    return Markup(s)


@dataclass
class RenderReport:
    pdf_path: Path
    pages: int
    content_height_px: float
    available_height_px: float
    entry_lines: list[int]
    line_grid_error: float  # max distance of any entry's height from a whole number of lines

    @property
    def spare_px(self) -> float:
        return self.available_height_px - self.content_height_px

    @property
    def fits(self) -> bool:
        return self.pages == 1 and self.spare_px >= 0


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(HERE / "templates")),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["inline"] = inline
    return env


def build_html(resume: Resume) -> str:
    env = _env()
    css = env.get_template("resume.css.j2").render(fonts_uri=FONTS_DIR.as_uri(), m=MARGINS)
    return env.get_template("resume.html.j2").render(resume=resume, css=Markup(css))


def render_resume(
    resume: Resume,
    out_pdf: Path,
    settings: Settings | None = None,
    work_dir: Path | None = None,
) -> RenderReport:
    from playwright.sync_api import sync_playwright  # imported late so config can set the browser path first

    settings = settings or load_settings()
    settings.ensure_dirs()
    apply_environment(settings)
    work_dir = Path(work_dir or settings.tmp_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    html_path = work_dir / "resume.html"
    html_path.write_text(build_html(resume), encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 816, "height": 1056})
            page.goto(html_path.as_uri())
            loaded = page.evaluate(
                "faces => Promise.all(faces.map(f => document.fonts.load(f).then(r => r.length > 0)))",
                list(_FONT_FACES),
            )
            if not all(loaded):
                raise RuntimeError("Bundled Tinos font failed to load; refusing to render with a fallback font")
            m = page.evaluate(_MEASURE_JS)
            page.pdf(path=str(out_pdf), prefer_css_page_size=True, print_background=True)
        finally:
            browser.close()

    lines = [e["lines"] for e in m["entries"]]
    return RenderReport(
        pdf_path=out_pdf,
        pages=len(PdfReader(str(out_pdf)).pages),
        content_height_px=m["height"],
        available_height_px=(PAGE_HEIGHT_IN - MARGINS["top"] - MARGINS["bottom"]) * PX_PER_IN,
        entry_lines=[round(x) for x in lines],
        line_grid_error=max((abs(x - round(x)) for x in lines), default=0.0),
    )


def pdf_text(pdf_path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
