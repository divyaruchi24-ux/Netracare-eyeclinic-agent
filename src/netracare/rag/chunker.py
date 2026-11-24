"""Heading-aware chunking of KB markdown.

Rules (docs/phase-3/understanding.md 3.4):
- every `#`/`##`/`###` heading starts a new section; a chunk never spans two sections;
- each chunk records its heading path below the H1 (e.g. "Prices > Surgery"); the document
  title is shown with every citation anyway;
- tables are never split mid-row; an oversized table is split into row groups that repeat
  the header;
- long prose is packed by paragraph up to `target_tokens` (hard cap `max_tokens`), with a
  one-sentence overlap;
- the trailing `---` + italic disclaimer footer is dropped (the UI always shows the disclaimer);
- `embed_text` = "title > heading path" + body, so short sections stay retrievable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from netracare.rag.types import Chunk, SourceDoc, chunk_id

CHUNKER_VERSION = "1"
PATH_SEP = " › "

_TOKEN = re.compile(r"\w+|[^\w\s]")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_FOOTER = re.compile(r"\n\s*-{3,}\s*\n(\s*\*[^\n]*\*\s*\n?)+\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*(\"'])")


def count_tokens(text: str) -> int:
    """Rough token count (words + punctuation); avoids a tokenizer dependency."""
    return len(_TOKEN.findall(text))


def strip_footer(body: str) -> str:
    return _FOOTER.sub("\n", body.rstrip() + "\n").rstrip() + "\n"


@dataclass
class _Section:
    path: list[str]
    lines: list[str]


def _sections(body: str, title: str) -> list[_Section]:
    stack: list[tuple[int, str]] = []
    sections: list[_Section] = [_Section([title], [])]
    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            sections.append(_Section([t for _, t in stack], []))
        else:
            sections[-1].lines.append(line)
    return [s for s in sections if any(line.strip() for line in s.lines)]


def _blocks(lines: list[str]) -> list[tuple[str, str]]:
    """Split section lines into ("table" | "text", content) blocks."""
    blocks: list[tuple[str, str]] = []
    buf: list[str] = []
    kind = "text"

    def flush() -> None:
        if buf and any(x.strip() for x in buf):
            blocks.append((kind, "\n".join(buf).strip()))
        buf.clear()

    for line in lines:
        is_table = line.lstrip().startswith("|")
        if not line.strip():
            flush()
            kind = "text"
            continue
        if is_table != (kind == "table") and buf:
            flush()
        kind = "table" if is_table else "text"
        buf.append(line)
    flush()
    return blocks


def _split_table(table: str, max_tokens: int) -> list[str]:
    rows = table.splitlines()
    header, body = rows[:2], rows[2:]
    groups: list[str] = []
    current: list[str] = []
    for row in body:
        candidate = "\n".join([*header, *current, row])
        if current and count_tokens(candidate) > max_tokens:
            groups.append("\n".join([*header, *current]))
            current = []
        current.append(row)
    if current or not groups:
        groups.append("\n".join([*header, *current]))
    return groups


def _split_text(text: str, max_tokens: int) -> list[str]:
    sentences = [s for s in _SENTENCE.split(text) if s.strip()]
    pieces: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        if current and count_tokens(" ".join([*current, sentence])) > max_tokens:
            pieces.append(" ".join(current))
            current = [current[-1]]  # one-sentence overlap
            if count_tokens(" ".join([*current, sentence])) > max_tokens:
                current = []
        current.append(sentence)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _pack(blocks: list[tuple[str, str]], target: int, max_tokens: int) -> list[str]:
    units: list[str] = []
    for kind, content in blocks:
        if count_tokens(content) <= max_tokens:
            units.append(content)
        elif kind == "table":
            units.extend(_split_table(content, max_tokens))
        else:
            units.extend(_split_text(content, max_tokens))
    chunks: list[str] = []
    current: list[str] = []
    for unit in units:
        candidate = "\n\n".join([*current, unit])
        if current and count_tokens(candidate) > target:
            chunks.append("\n\n".join(current))
            current = []
        current.append(unit)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk(doc: SourceDoc, target_tokens: int = 250, max_tokens: int = 400) -> list[Chunk]:
    body = strip_footer(doc.body)
    out: list[Chunk] = []
    for section in _sections(body, doc.title):
        # The H1 repeats the document title (always shown with a citation), so drop it
        # from deeper paths to keep them short: "What does it cost?" not "LASIK FAQ > ...".
        path = section.path[1:] if len(section.path) > 1 else section.path
        heading_path = PATH_SEP.join(path)
        for text in _pack(_blocks(section.lines), target_tokens, max_tokens):
            ordinal = len(out)
            embed_text = f"{doc.title}{PATH_SEP}{heading_path}\n\n{text}"
            out.append(
                Chunk(
                    id=chunk_id(doc.slug, doc.version, ordinal),
                    slug=doc.slug,
                    version=doc.version,
                    ordinal=ordinal,
                    heading_path=heading_path,
                    text=text,
                    embed_text=embed_text,
                    token_count=count_tokens(text),
                )
            )
    return out
