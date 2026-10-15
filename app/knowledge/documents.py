"""Find and load source documents.

Layout under the docs root:
    product/...               shared product documentation (visible to every tenant)
    tenants/<tenant_id>/...   documentation visible to that tenant only
"""

import hashlib
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from pypdf import PdfReader

SUPPORTED = {".md", ".markdown", ".html", ".htm", ".pdf", ".txt"}
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class SourceDoc:
    uid: str  # "global:<path>" or "<tenant_id>:<path>"
    path: str  # relative to the docs root, forward slashes
    tenant_id: str | None
    title: str
    text: str  # normalised to Markdown-style headings
    content_hash: str
    format: str


class _HtmlToMarkdown(HTMLParser):
    """Keep headings (as #, ##, ###) and paragraph text; drop markup, scripts and styles."""

    _HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "###"}
    _BLOCKS = {"p", "li", "div", "section", "article", "tr", "br", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._HEADINGS:
            self.parts.append(f"\n\n{self._HEADINGS[tag]} ")
        elif tag in self._BLOCKS:
            self.parts.append("\n\n" if tag != "li" else "\n- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._HEADINGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> tuple[str, str]:
    parser = _HtmlToMarkdown()
    parser.feed(html)
    text = re.sub(r"[ \t]+\n", "\n", "".join(parser.parts))
    return parser.title.strip(), re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_to_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(f"## Page {i}\n\n{text}" for i, text in enumerate(pages, start=1) if text)


def _title_from(text: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def load(root: Path, path: Path) -> SourceDoc | None:
    rel = path.relative_to(root).as_posix()
    parts = rel.split("/")
    tenant_id = parts[1] if len(parts) > 2 and parts[0] == "tenants" and _UUID.match(parts[1]) else None
    if parts[0] == "tenants" and tenant_id is None:
        return None  # tenants/ must be followed by a tenant UUID
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    fallback_title = path.stem.replace("-", " ").replace("_", " ").capitalize()
    if suffix in (".html", ".htm"):
        html_title, text = html_to_text(raw.decode("utf-8", errors="replace"))
        title = html_title or _title_from(text, fallback_title)
    elif suffix == ".pdf":
        text = pdf_to_text(raw)
        title = fallback_title
    else:
        text = raw.decode("utf-8", errors="replace")
        title = _title_from(text, fallback_title)
    if not text.strip():
        return None
    return SourceDoc(
        uid=f"{tenant_id or 'global'}:{rel}",
        path=rel,
        tenant_id=tenant_id,
        title=title,
        text=text,
        content_hash=hashlib.sha256(raw).hexdigest(),
        format=suffix.lstrip("."),
    )


def discover(root: Path) -> list[SourceDoc]:
    docs = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            doc = load(root, path)
            if doc is not None:
                docs.append(doc)
    return docs
