from collections.abc import Callable
from pathlib import Path

import pytest

from netracare.rag.loaders import load, load_pdf, pdf_text_to_markdown, scan
from netracare.seed.kb import FrontMatterError
from tests.fixtures_rag import KB_DIR, make_pdf, md_doc

WriteKb = Callable[[dict[str, str | bytes]], Path]

PDF_META = (
    "slug: visitor-guide\ntitle: Visitor guide\ndoc_type: general_faq\n"
    "version: 1\neffective_date: 2026-02-01\n"
)


def test_real_kb_loads_with_metadata() -> None:
    docs = scan(KB_DIR)
    assert len(docs) >= 23
    assert {d.kind for d in docs} == {"md"}
    lasik = next(d for d in docs if d.slug == "procedure-lasik")
    assert lasik.meta["doctor_code"] == "D1"
    assert lasik.body.startswith("# LASIK")


def test_pdf_with_sidecar_loads(write_kb: WriteKb) -> None:
    pdf = make_pdf(
        [
            "VISITOR GUIDE",
            "Parking and Access",
            "Free parking is available in the basement for patients.",
            "Where do I wait?",
            "The waiting area is on the ground floor near reception.",
        ]
    )
    kb = write_kb({"guides/visitor.pdf": pdf, "guides/visitor.meta.yaml": PDF_META})
    doc = load_pdf(kb / "guides" / "visitor.pdf")
    assert doc.kind == "pdf"
    assert doc.slug == "visitor-guide"
    assert "## Parking and Access" in doc.body
    assert "## Where do I wait?" in doc.body
    assert "Free parking is available" in doc.body


def test_pdf_without_sidecar_is_rejected(write_kb: WriteKb) -> None:
    kb = write_kb({"x.pdf": make_pdf(["Hello"])})
    with pytest.raises(FrontMatterError, match="missing sidecar"):
        load(kb / "x.pdf")


def test_pdf_sidecar_is_validated(write_kb: WriteKb) -> None:
    kb = write_kb({"x.pdf": make_pdf(["Hello"]), "x.meta.yaml": "slug: x\n"})
    with pytest.raises(FrontMatterError, match="missing keys"):
        load(kb / "x.pdf")


def test_bad_markdown_front_matter_names_the_file(write_kb: WriteKb) -> None:
    kb = write_kb({"bad.md": md_doc("bad", "# Hi", doc_type="nonsense")})
    with pytest.raises(FrontMatterError, match=r"bad\.md.*unknown doc_type"):
        scan(kb)


def test_unsupported_suffix() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        load(Path("notes.txt"))


def test_pdf_heading_heuristic() -> None:
    md = pdf_text_to_markdown(
        "Guide",
        "Guide\nOPENING HOURS\nWe open at nine\nand close at six.\n\nIs there a lift?\nYes.",
    )
    assert "## OPENING HOURS" in md
    assert "We open at nine and close at six." in md
    assert "## Is there a lift?" in md
    assert "## Guide" not in md  # the title line is not repeated as a heading
