from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.exports.view import MeetingView

_TEMPLATES = Path(__file__).resolve().parent / "templates"


@lru_cache
def _env() -> Environment:
    return Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=select_autoescape(["html"]))


def render_html(view: MeetingView) -> str:
    return _env().get_template("meeting.html").render(v=view)


def render_pdf(view: MeetingView) -> bytes:
    # WeasyPrint shapes Arabic and lays out right-to-left text through Pango/HarfBuzz.
    # Imported lazily because it is slow to import and only needed for exports.
    from weasyprint import HTML

    return HTML(string=render_html(view)).write_pdf()
