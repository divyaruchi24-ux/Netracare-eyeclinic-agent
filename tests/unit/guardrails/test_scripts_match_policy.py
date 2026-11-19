"""The patient-facing scripts must equal docs/safety_policy.md character for character
(markdown `> ` quote markers aside)."""

from __future__ import annotations

import re

import pytest

from netracare.guardrails import scripts
from tests.conftest import ROOT

POLICY = (ROOT / "docs" / "safety_policy.md").read_text(encoding="utf-8")


def _section(start: str, end: str) -> str:
    return POLICY[POLICY.index(start) : POLICY.index(end, POLICY.index(start))]


def _quoted(block: str) -> list[str]:
    return [line[2:] for line in block.splitlines() if line.startswith("> ")]


def test_emergency_script_verbatim() -> None:
    block = _section("### 5.1 Emergency script", "For **chemical splash (E4)** only")
    assert "\n".join(_quoted(block)) == scripts.EMERGENCY_SCRIPT


def test_chemical_rinse_line_verbatim_and_placed_before_bullets() -> None:
    block = _section("For **chemical splash (E4)** only", "This is the **only** first-aid")
    (line,) = _quoted(block)
    assert line == scripts.CHEMICAL_RINSE_LINE
    msg = scripts.emergency_message(["E4", "E1"])
    assert msg.index(scripts.CHEMICAL_RINSE_LINE) < msg.index("• Call the")
    assert msg.startswith(scripts.EMERGENCY_HEADLINE)
    assert msg.replace(scripts.CHEMICAL_RINSE_LINE + "\n", "") == scripts.EMERGENCY_SCRIPT


def test_non_chemical_emergency_has_no_rinse_line() -> None:
    assert scripts.emergency_message(["E1"]) == scripts.EMERGENCY_SCRIPT
    assert scripts.CHEMICAL_RINSE_LINE not in scripts.emergency_message([])


@pytest.mark.parametrize(
    ("start", "end", "constant"),
    [
        ("### 5.2 Urgent script", "### 5.3", scripts.URGENT_SCRIPT),
        ("### 5.3 Medical-question refusal", "### 5.4", scripts.MEDICAL_REFUSAL),
        ("### 5.4 Unknown or low-confidence", "## 6.", scripts.LOW_CONFIDENCE),
    ],
)
def test_other_scripts_verbatim(start: str, end: str, constant: str) -> None:
    # The policy names the 5.3 placeholder in prose; the constant uses a format field.
    expected = constant.replace("{routing_sentence}", "{routing sentence from §6}")
    assert "\n".join(_quoted(_section(start, end))) == expected


def test_routing_templates_match_section_6() -> None:
    block = _section("## 6. Non-diagnostic routing phrasing", "## 7.")
    used = re.findall(r'^\| "(.+?)" \|', block, flags=re.MULTILINE)
    assert scripts.ROUTING_TEMPLATES["specialty"] == used[0]
    assert scripts.ROUTING_TEMPLATES["pediatric"] == used[1]
    assert scripts.ROUTING_TEMPLATES["general"] == used[2]
    assert used[3] == scripts.CHECK_AVAILABILITY


def test_disclaimers_match_section_10() -> None:
    block = _section("## 10. Disclaimer texts", "## 11.")
    texts = set(re.findall(r'^\| [^|]+ \| "(.+?)"(?: \([^)]*\))? \|$', block, flags=re.MULTILINE))
    assert set(scripts.DISCLAIMERS.values()) == texts


def test_routing_sentence() -> None:
    s = scripts.routing_sentence("RETINA", "new floaters", "Dr. Vikram Shetty")
    assert s == (
        "Concerns about **new floaters** are typically seen by our **Retina** specialist, "
        "**Dr. Vikram Shetty**."
    )
    assert scripts.routing_sentence("PEDIATRIC", "x", "y") == scripts.ROUTING_TEMPLATES["pediatric"]
    general = scripts.ROUTING_TEMPLATES["general"]
    assert scripts.routing_sentence("GENERAL", "itchy eyes", "Dr. Kavya Nair") == general
    assert scripts.routing_sentence(None, None, None) == general
    assert scripts.routing_sentence("RETINA", "", "Dr. X") == general  # no concern -> general
    assert scripts.routing_sentence("UNKNOWN", "x", "Dr. X") == general
