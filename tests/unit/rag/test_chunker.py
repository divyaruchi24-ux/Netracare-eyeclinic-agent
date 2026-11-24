import itertools
from datetime import date
from pathlib import Path

from netracare.rag.chunker import PATH_SEP, chunk, count_tokens, strip_footer
from netracare.rag.loaders import scan
from netracare.rag.types import SourceDoc, chunk_id
from tests.fixtures_rag import KB_DIR


def _doc(body: str, title: str = "Doc") -> SourceDoc:
    meta = {
        "slug": "doc",
        "title": title,
        "doc_type": "general_faq",
        "version": 1,
        "effective_date": date(2026, 1, 1),
    }
    return SourceDoc(Path("doc.md"), meta, body, "x")


def test_every_real_kb_doc_chunks_within_limits() -> None:
    for doc in scan(KB_DIR):
        chunks = chunk(doc)
        assert chunks, doc.slug
        for c in chunks:
            assert c.heading_path
            assert 0 < c.token_count <= 400
            assert c.embed_text.startswith(doc.title)
            assert "Clinic-approved general information" not in c.text  # footer stripped
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_price_table_stays_whole() -> None:
    doc = next(d for d in scan(KB_DIR) if d.slug == "price-list" and d.version == 2)
    chunks = chunk(doc)
    table = [c for c in chunks if "| LASIK (both eyes) | 52,000 |" in c.text]
    assert len(table) == 1
    assert "| Oculoplasty consultation | 1,000 |" in table[0].text
    assert table[0].text.count("| Service | Price (INR) |") == 1


def test_heading_path_and_sections_never_merge() -> None:
    body = (
        "# Doc\n\nIntro line.\n\n## First\n\nAlpha.\n\n### Deeper\n\nBeta.\n\n## Second\n\nGamma.\n"
    )
    chunks = chunk(_doc(body))
    assert [c.heading_path for c in chunks] == [
        "Doc",
        "First",
        f"First{PATH_SEP}Deeper",
        "Second",
    ]
    assert [c.text for c in chunks] == ["Intro line.", "Alpha.", "Beta.", "Gamma."]
    assert chunks[2].embed_text == f"Doc{PATH_SEP}First{PATH_SEP}Deeper\n\nBeta."


def test_empty_heading_sections_are_skipped() -> None:
    chunks = chunk(_doc("# Doc\n\n## Empty\n\n## Full\n\nText here.\n"))
    assert [c.heading_path for c in chunks] == ["Full"]


def test_long_prose_splits_with_one_sentence_overlap() -> None:
    sentences = [f"Sentence number {i} talks about clinic logistics in detail." for i in range(60)]
    chunks = chunk(_doc("## Long\n\n" + " ".join(sentences)), target_tokens=60, max_tokens=80)
    assert len(chunks) > 3
    assert all(c.token_count <= 80 for c in chunks)
    for a, b in itertools.pairwise(chunks):
        last = a.text.split(". ")[-1].rstrip(".")
        assert b.text.startswith(last)


def test_oversized_table_splits_by_rows_with_header() -> None:
    rows = "\n".join(f"| Item {i} | {i * 100} |" for i in range(80))
    body = f"## Prices\n\n| Service | Price |\n|---|---|\n{rows}\n"
    chunks = chunk(_doc(body), target_tokens=100, max_tokens=120)
    assert len(chunks) > 1
    for c in chunks:
        assert c.text.startswith("| Service | Price |\n|---|---|")
        assert c.token_count <= 120
    all_rows = [r for c in chunks for r in c.text.splitlines()[2:]]
    assert len(all_rows) == 80


def test_strip_footer_only_removes_trailing_disclaimer() -> None:
    body = "Text.\n\n---\n\nMore.\n\n---\n*Clinic-approved general information.*\n"
    assert strip_footer(body).strip() == "Text.\n\n---\n\nMore."


def test_chunk_ids_are_stable() -> None:
    a = chunk(_doc("## A\n\nx"))[0]
    assert a.id == chunk_id("doc", 1, 0)
    assert len(a.id) == 32


def test_count_tokens() -> None:
    assert count_tokens("LASIK costs 52,000.") == 6
