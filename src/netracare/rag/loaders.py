"""Load knowledge-base files: markdown with YAML front matter, or PDF plus a sidecar YAML.

A PDF has no front matter, so its metadata lives next to it in `<stem>.meta.yaml`
(same keys as markdown front matter). PDF text is turned into pseudo-markdown, with
heading-like lines promoted to `##` headings, so the same chunker handles both formats.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from netracare.rag.types import SourceDoc
from netracare.seed.kb import FrontMatterError, parse_front_matter, validate_meta

SUPPORTED_SUFFIXES = (".md", ".pdf")
_MAX_HEADING_CHARS = 70


def load_markdown(path: Path) -> SourceDoc:
    raw = path.read_text(encoding="utf-8")
    try:
        meta, body = parse_front_matter(raw)
    except FrontMatterError as exc:
        raise FrontMatterError(f"{path}: {exc}") from exc
    return SourceDoc(path, meta, body, hashlib.sha256(raw.encode()).hexdigest(), "md")


def sidecar_path(pdf: Path) -> Path:
    return pdf.with_name(f"{pdf.stem}.meta.yaml")


def load_pdf(path: Path) -> SourceDoc:
    from pypdf import PdfReader

    meta_file = sidecar_path(path)
    if not meta_file.exists():
        raise FrontMatterError(f"{path}: missing sidecar metadata {meta_file.name}")
    try:
        meta = validate_meta(yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {})
    except FrontMatterError as exc:
        raise FrontMatterError(f"{meta_file}: {exc}") from exc
    data = path.read_bytes()
    reader = PdfReader(path)
    pages = [page.extract_text() or "" for page in reader.pages]
    body = pdf_text_to_markdown(str(meta["title"]), "\n\n".join(pages))
    checksum = hashlib.sha256(data + meta_file.read_bytes()).hexdigest()
    return SourceDoc(path, meta, body, checksum, "pdf")


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > _MAX_HEADING_CHARS or s.endswith((".", ",", ";", ":")):
        return False
    if s.endswith("?"):
        return True  # FAQ-style question headings
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&-]*", s)]
    if not words or len(words) > 10:
        return False
    if s.isupper():
        return True
    significant = [w for w in words if len(w) > 3]
    return bool(significant) and all(w[0].isupper() for w in significant)


def pdf_text_to_markdown(title: str, text: str) -> str:
    """Promote heading-like lines to `##` and re-join wrapped paragraph lines."""
    out: list[str] = [f"# {title}", ""]
    para: list[str] = []

    def flush() -> None:
        if para:
            out.extend([" ".join(para), ""])
            para.clear()

    for line in text.splitlines():
        s = line.strip()
        if not s:
            flush()
        elif _is_heading(s) and s.lower() != title.lower():
            flush()
            out.extend([f"## {s}", ""])
        else:
            para.append(s)
    flush()
    return "\n".join(out).strip() + "\n"


def load(path: Path) -> SourceDoc:
    if path.suffix == ".md":
        return load_markdown(path)
    if path.suffix == ".pdf":
        return load_pdf(path)
    raise ValueError(f"unsupported KB file type: {path}")


def scan(kb_dir: Path) -> list[SourceDoc]:
    """Every supported file under `kb_dir`, sorted by path for deterministic ingestion."""
    paths = sorted(p for p in kb_dir.rglob("*") if p.suffix in SUPPORTED_SUFFIXES and p.is_file())
    return [load(p) for p in paths]
