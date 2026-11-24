"""Register knowledge-base markdown files (front-matter metadata) as KnowledgeDoc rows.

Chunking and indexing happen in Phase 3; here we only track documents and versions.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from netracare.db.models import KnowledgeDoc
from netracare.domain.enums import DocType
from netracare.repositories.directory_repo import KnowledgeDocRepository
from netracare.seed.ids import seed_id

REQUIRED_KEYS = ("slug", "title", "doc_type", "version", "effective_date")


class FrontMatterError(ValueError):
    pass


@dataclass(frozen=True)
class KbFile:
    path: Path
    meta: dict[str, Any]
    body: str
    checksum: str


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise FrontMatterError("missing front-matter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise FrontMatterError("unterminated front-matter")
    meta = validate_meta(yaml.safe_load(parts[1]) or {})
    return meta, parts[2].lstrip("\n")


def validate_meta(meta: object) -> dict[str, Any]:
    """Check KB metadata (markdown front matter or a PDF's sidecar YAML)."""
    if not isinstance(meta, dict):
        raise FrontMatterError("front-matter must be a mapping")
    missing = [k for k in REQUIRED_KEYS if k not in meta]
    if missing:
        raise FrontMatterError(f"missing keys: {missing}")
    try:
        DocType(meta["doc_type"])
    except ValueError as exc:
        raise FrontMatterError(f"unknown doc_type {meta['doc_type']!r}") from exc
    if not isinstance(meta["effective_date"], date):
        raise FrontMatterError("effective_date must be a YYYY-MM-DD date")
    return meta


def scan_kb(kb_dir: Path) -> list[KbFile]:
    files: list[KbFile] = []
    for path in sorted(kb_dir.rglob("*.md")):
        raw = path.read_text(encoding="utf-8")
        try:
            meta, body = parse_front_matter(raw)
        except FrontMatterError as exc:
            raise FrontMatterError(f"{path}: {exc}") from exc
        files.append(KbFile(path, meta, body, hashlib.sha256(raw.encode()).hexdigest()))
    return files


def upsert_kb(session: Session, kb_dir: Path, root: Path) -> int:
    """Insert new (slug, version) docs, update checksum of changed ones, link supersession.

    Returns the number of newly inserted docs.
    """
    repo = KnowledgeDocRepository(session)
    inserted = 0
    for f in scan_kb(kb_dir):
        slug, version = str(f.meta["slug"]), int(f.meta["version"])
        extra = {k: v for k, v in f.meta.items() if k not in REQUIRED_KEYS}
        rel = f.path.relative_to(root).as_posix()
        existing = repo.by_slug_version(slug, version)
        if existing is None:
            session.add(
                KnowledgeDoc(
                    id=seed_id("kb", f"{slug}@{version}"),
                    slug=slug,
                    title=str(f.meta["title"]),
                    doc_type=DocType(f.meta["doc_type"]),
                    version=version,
                    effective_date=f.meta["effective_date"],
                    source_path=rel,
                    checksum=f.checksum,
                    meta=extra,
                )
            )
            inserted += 1
        elif existing.checksum != f.checksum:
            existing.checksum = f.checksum
            existing.title = str(f.meta["title"])
            existing.meta = extra
    session.flush()
    link_supersession(session)
    return inserted


def link_supersession(session: Session) -> None:
    by_slug: dict[str, list[KnowledgeDoc]] = {}
    for doc in KnowledgeDocRepository(session).list_all():
        by_slug.setdefault(doc.slug, []).append(doc)
    for docs in by_slug.values():
        docs.sort(key=lambda d: (d.effective_date, d.version))
        for older, newer in itertools.pairwise(docs):
            if older.superseded_by_id != newer.id:
                older.superseded_by_id = newer.id
        if docs[-1].superseded_by_id is not None:
            docs[-1].superseded_by_id = None
    session.flush()
