from __future__ import annotations

import pytest
import yaml

from netracare.guardrails.advice_filter import check_fact, check_summary, safe_concern
from tests.conftest import ROOT

TOPICS = {
    t["id"]: t
    for t in yaml.safe_load((ROOT / "prompts/eye_fact/topics.yaml").read_text(encoding="utf-8"))[
        "topics"
    ]
}
FACTS = yaml.safe_load((ROOT / "prompts/eye_fact/static_facts.yaml").read_text(encoding="utf-8"))[
    "facts"
]


def test_one_static_fact_per_topic() -> None:
    assert set(TOPICS) == set(FACTS)
    assert len(TOPICS) >= 20


@pytest.mark.parametrize("topic_id", sorted(TOPICS))
def test_static_facts_pass_the_filter(topic_id: str) -> None:
    assert check_fact(FACTS[topic_id], TOPICS[topic_id]["keywords"]) == []


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Use artificial tears to keep your eyes moist.", "advice_directive"),
        ("You should take vitamin A for a healthy retina.", "advice_directive"),
        ("The retina loves omega supplements.", "medicine"),
        ("Glaucoma is a leading cause of blindness in the retina.", "diagnosis_or_risk"),
        ("The cornea is 20% water by weight.", "number_unit"),
        ("Read more about the iris at www.eyes.example", "url"),
        ("Carrots are orange and crunchy.", "off_topic"),
        ("Avoid rubbing your eyes; the cornea is delicate.", "advice_directive"),
        ("", "empty"),
        ("The cornea " + "is very clear " * 20, "too_long"),
    ],
)
def test_unsafe_facts_rejected(text: str, code: str) -> None:
    assert code in check_fact(text, ["cornea", "retina", "iris"])


NOTES = (
    "Vision 6/6 both eyes. IOP 14 mmHg OU. Mild dry eye. Start carboxymethylcellulose 0.5% "
    "1 drop 4 times daily both eyes. Review in 3 months."
)
GOOD = {
    "what_we_checked": ["vision", "eye pressure"],
    "what_we_found": "Your vision is 6/6 and your eye pressure is 14 mmHg. You have mild dry eye.",
    "next_steps": ["Use the drops as written below."],
    "medicines_mentioned": [
        {
            "name": "carboxymethylcellulose 0.5%",
            "instructions_verbatim": "1 drop 4 times daily both eyes",
        }
    ],
    "follow_up": "Review in 3 months.",
    "unclear_in_notes": [],
}


def test_faithful_summary_has_no_flags() -> None:
    assert check_summary(GOOD, NOTES) == []


def test_invented_items_are_flagged() -> None:
    bad = GOOD | {
        "what_we_found": "Pressure 18 mmHg, early glaucoma. Take 0.5 mg.",
        "medicines_mentioned": [{"name": "timolol", "instructions_verbatim": "twice daily"}],
        "what_we_checked": [],
    }
    flags = check_summary(bad, NOTES)
    assert "medicine not in notes: timolol" in flags
    assert "medicine instructions not verbatim: timolol" in flags
    assert "number not in notes: 18 mmHg" in flags
    assert "unit differs from notes: 0.5 mg" in flags
    assert "condition not in notes: glaucoma" in flags
    assert "empty section: what_we_checked" in flags
    assert sum("timolol" in f for f in flags) == 2  # no duplicate "possible medicine" flag


def test_unlisted_drug_like_word_flagged() -> None:
    bad = GOOD | {"next_steps": ["Continue moxifloxacin as before."]}
    assert "possible medicine not in notes: moxifloxacin" in check_summary(bad, NOTES)


def test_empty_found_flagged() -> None:
    assert "empty section: what_we_found" in check_summary(GOOD | {"what_we_found": " "}, NOTES)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("new floaters", "new floaters"),
        ("Itchy eyes.", "itchy eyes"),
        ("retinal detachment", None),
        ("possible glaucoma", None),
        ("take drops", None),
        ("a" * 70, None),
        ("eye pain 3 days", None),
        (None, None),
        ("", None),
    ],
)
def test_safe_concern(phrase: str | None, expected: str | None) -> None:
    assert safe_concern(phrase) == expected
