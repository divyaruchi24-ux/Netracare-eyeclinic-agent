"""RAG test fixtures: a migrated empty DB (built once, copied per test), fixture-KB writers,
and a minimal PDF generator. No model downloads: tests use HashingEmbedder + InMemoryVectorStore.
"""

from __future__ import annotations

import shutil
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.orm import Session, sessionmaker

from netracare.db.session import make_engine, make_session_factory
from netracare.rag.vector_store import InMemoryVectorStore
from tests.conftest import ROOT, alembic_config

KB_DIR = ROOT / "kb"


@pytest.fixture(scope="session")
def empty_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("rag") / "empty.db"
    command.upgrade(alembic_config(f"sqlite:///{path.as_posix()}"), "head")
    return path


@pytest.fixture
def rag_factory(empty_db_template: Path, tmp_path: Path) -> sessionmaker[Session]:
    target = tmp_path / "rag.db"
    shutil.copyfile(empty_db_template, target)
    return make_session_factory(make_engine(f"sqlite:///{target.as_posix()}"))


def md_doc(
    slug: str,
    body: str,
    *,
    title: str | None = None,
    doc_type: str = "general_faq",
    version: int = 1,
    effective: str = "2026-01-01",
    extra: str = "",
) -> str:
    head = (
        f"---\nslug: {slug}\ntitle: {title or slug}\ndoc_type: {doc_type}\n"
        f"version: {version}\neffective_date: {effective}\n{extra}---\n\n"
    )
    return head + textwrap.dedent(body).strip() + "\n"


@pytest.fixture
def write_kb(tmp_path: Path) -> Callable[[dict[str, str | bytes]], Path]:
    """Write `{relative path: content}` into a fresh KB dir and return it."""

    def _write(files: dict[str, str | bytes]) -> Path:
        kb = tmp_path / "kb"
        for rel, content in files.items():
            p = kb / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
        return kb

    return _write


def make_pdf(lines: list[str]) -> bytes:
    """A minimal single-page PDF (Helvetica text, one line per entry) that pypdf can read."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
    for line in lines:
        ops += [f"({esc(line)}) Tj", "T*"]
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


@pytest.fixture(scope="session")
def ingested_template(seeded_template: Path) -> Path:
    """The seeded API template DB; it already has the real `kb/` ingested (hashing vectors)."""
    return seeded_template


def hashing_store_from_db(factory: sessionmaker[Session]) -> InMemoryVectorStore:
    """An in-memory store matching `kb_chunks` (same vectors the ingest would have written)."""
    from sqlalchemy import select

    from netracare.db.models import KbChunk, KbIndexState
    from netracare.rag.embeddings import HashingEmbedder
    from netracare.rag.ingest import vector_meta

    store = InMemoryVectorStore()
    with factory() as s:
        rows = list(s.scalars(select(KbChunk)))
        state = s.get(KbIndexState, 1)
        emb = HashingEmbedder()
        store.upsert(
            [r.id for r in rows],
            emb.embed_documents([r.embed_text for r in rows]),
            [vector_meta(r) for r in rows],
        )
        if state is not None:
            store.set_fingerprint(state.fingerprint)
    return store


@pytest.fixture
def kb_factory(ingested_template: Path, tmp_path: Path) -> sessionmaker[Session]:
    target = tmp_path / "kb.db"
    shutil.copyfile(ingested_template, target)
    return make_session_factory(make_engine(f"sqlite:///{target.as_posix()}"))
