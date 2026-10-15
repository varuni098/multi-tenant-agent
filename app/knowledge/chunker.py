"""Split documents into heading-scoped chunks of about 500 tokens, with overlap."""

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*$")


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str  # "Section" or "Section › Subsection"
    text: str


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading path, body) per section. Content before the first heading uses the document title."""
    sections: list[tuple[str, str]] = []
    stack: list[str] = []
    current: list[str] = []

    def flush() -> None:
        body = "\n".join(current).strip()
        if body:
            sections.append((" › ".join(stack[1:]) or (stack[0] if stack else ""), body))
        current.clear()

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            stack[:] = stack[: level - 1] + [m.group(2).strip()]
        else:
            current.append(line)
    flush()
    return sections


def _windows(body: str, target: int, overlap: int) -> list[str]:
    """Pack paragraphs into ~target-character windows; overlap carries the tail of the previous window."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    windows: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > target:  # a single huge paragraph: hard split
            head, para = para[:target], para[target - overlap :]
            if current:
                windows.append(current)
                current = ""
            windows.append(head)
        if current and len(current) + len(para) + 2 > target:
            windows.append(current)
            current = current[-overlap:].split(" ", 1)[-1] + "\n\n" + para if overlap else para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        windows.append(current)
    return windows


def chunk(text: str, target_chars: int = 1800, overlap_chars: int = 200) -> list[Chunk]:
    """~1800 characters is roughly 450-500 tokens for English prose."""
    chunks: list[Chunk] = []
    for heading, body in _sections(text):
        for window in _windows(body, target_chars, overlap_chars):
            chunks.append(Chunk(index=len(chunks), heading=heading, text=window))
    return chunks
