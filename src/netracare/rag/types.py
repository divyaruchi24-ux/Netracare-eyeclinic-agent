"""Plain data types shared by the RAG pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

# Metadata keys copied from a document's front matter onto each of its chunks.
CHUNK_META_KEYS = ("doctor_code", "specialty", "procedure")


@dataclass(frozen=True)
class SourceDoc:
    """One knowledge-base file after loading (markdown body, or text extracted from a PDF)."""

    path: Path
    meta: dict[str, Any]
    body: str
    checksum: str
    kind: Literal["md", "pdf"] = "md"

    @property
    def slug(self) -> str:
        return str(self.meta["slug"])

    @property
    def version(self) -> int:
        return int(self.meta["version"])

    @property
    def title(self) -> str:
        return str(self.meta["title"])

    @property
    def effective_date(self) -> date:
        value = self.meta["effective_date"]
        if not isinstance(value, date):
            raise TypeError(f"{self.path}: effective_date must be a date")
        return value

    @property
    def trusted_override(self) -> bool:
        """Set in front matter to index a doc whose text trips the injection scanner."""
        return bool(self.meta.get("trusted_override", False))


def chunk_id(slug: str, version: int, ordinal: int) -> str:
    return hashlib.sha256(f"{slug}@{version}#{ordinal}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Chunk:
    id: str
    slug: str
    version: int
    ordinal: int
    heading_path: str
    text: str
    embed_text: str
    token_count: int


@dataclass(frozen=True)
class RefusedChunk:
    slug: str
    ordinal: int
    reasons: tuple[str, ...]


@dataclass
class IngestReport:
    docs_seen: int = 0
    docs_changed: int = 0
    docs_removed: int = 0
    chunks_total: int = 0
    chunks_written: int = 0
    chunks_deleted: int = 0
    refused: list[RefusedChunk] = field(default_factory=list)
    fingerprint: str = ""
    rebuilt_vectors: bool = False
    duration_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "docs_seen": self.docs_seen,
            "docs_changed": self.docs_changed,
            "docs_removed": self.docs_removed,
            "chunks_total": self.chunks_total,
            "chunks_written": self.chunks_written,
            "chunks_deleted": self.chunks_deleted,
            "refused": [
                {"slug": r.slug, "ordinal": r.ordinal, "reasons": list(r.reasons)}
                for r in self.refused
            ],
            "fingerprint": self.fingerprint,
            "rebuilt_vectors": self.rebuilt_vectors,
            "duration_ms": self.duration_ms,
        }
