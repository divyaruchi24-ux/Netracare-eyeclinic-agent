import re

import pytest

from netracare.agents.specialist import (
    Lexicon,
    RoutingOutcome,
    SpecialistRouter,
    load_lexicon,
    parse_lexicon,
)
from netracare.guardrails import scripts
from tests.conftest import ROOT

DOCS = {
    "CORNEA": ("d1", "Dr. Ananya Rao"),
    "RETINA": ("d2", "Dr. Vikram Shetty"),
    "GLAUCOMA": ("d3", "Dr. Meera Iyer"),
    "PEDIATRIC": ("d4", "Dr. Arjun Menon"),
    "GENERAL": ("d5", "Dr. Kavya Nair"),
}
LEX = load_lexicon(str(ROOT / "config" / "specialty_routing.yaml"))

# Every sentence must be one of the section 6 templates (filled in).
_SPECIALTY = re.escape(scripts.ROUTING_TEMPLATES["specialty"])
_SPECIALTY = _SPECIALTY.replace(r"\{concern\}", ".+").replace(r"\{specialty\}", ".+")
_SPECIALTY = _SPECIALTY.replace(r"\{doctor\}", "Dr\\. .+")
SECTION_6 = re.compile(
    "^(?:"
    + "|".join(
        [_SPECIALTY, re.escape(scripts.ROUTING_TEMPLATES["pediatric"]),
         re.escape(scripts.ROUTING_TEMPLATES["general"])]
    )
    + ")$"
)  # fmt: skip


def router() -> SpecialistRouter:
    return SpecialistRouter(DOCS.get, LEX)


def _check(o: RoutingOutcome) -> RoutingOutcome:
    if o.kind in ("specialty", "pediatric", "general"):
        assert o.sentence and SECTION_6.match(o.sentence), o.sentence
    return o


def test_red_flag_code_routes_with_policy_concern() -> None:
    o = _check(router().route("a few new floaters", codes=["U1"]))
    assert (o.kind, o.specialty, o.doctor_name) == ("specialty", "RETINA", "Dr. Vikram Shetty")
    assert "**new floaters**" in (o.sentence or "")


@pytest.mark.parametrize(
    "text", ["my 6 year old needs a check-up", "my daughter keeps rubbing her eyes"]
)
def test_child_mentions_go_to_pediatrics(text: str) -> None:
    o = _check(router().route(text, llm_hint="CORNEA"))
    assert o.kind == "pediatric" and o.doctor_id == "d4"


def test_pediatric_codes_win() -> None:
    assert router().route("white pupil in photos", codes=["U4"]).kind == "pediatric"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("my eyes feel dry and gritty", "CORNEA"),
        ("I wear contact lenses and want LASIK", "CORNEA"),
        ("I'm diabetic and need an eye check", "RETINA"),
        ("family history of glaucoma", "GLAUCOMA"),
        ("droopy eyelid", "GENERAL"),
        ("cloudy vision, maybe a cataract", "GENERAL"),
    ],
)
def test_lexicon_routes(text: str, code: str) -> None:
    o = _check(router().route(text))
    assert o.specialty == code and o.doctor_id == DOCS[code][0]


def test_general_concern_is_named_not_called_ambiguous() -> None:
    o = router().route("droopy eyelid")
    assert o.kind == "specialty"
    assert scripts.GENERAL_SPECIALTY_LABEL in (o.sentence or "")


def test_llm_hint_used_when_lexicon_is_silent() -> None:
    o = _check(router().route("book a visit", llm_hint="RETINA", concern_phrase="blurry patches"))
    assert o.specialty == "RETINA" and "**blurry patches**" in (o.sentence or "")


def test_llm_concern_is_sanitised() -> None:
    o = router().route("dry eyes", concern_phrase="early keratoconus")
    assert "keratoconus" not in (o.sentence or "")


def test_ambiguity_asks_once_then_goes_general() -> None:
    first = router().route("dry eyes and some floaters")
    assert first.kind == "clarify" and len(first.clarify_options) == 2
    assert first.sentence and first.sentence.startswith("To point you to the right specialist")
    assert first.pending == {"kind": "specialty", "options": list(first.clarify_options)}
    unsure = _check(router().route("not sure really", pending=first.pending))
    assert unsure.kind == "general" and unsure.doctor_id == "d5"
    resolved = _check(router().route("mostly the floaters", pending=first.pending))
    assert resolved.specialty == "RETINA"
    still = _check(router().route("both dry eyes and floaters", pending=first.pending))
    assert still.kind == "general"


def test_lexicon_and_llm_disagreement_is_ambiguous() -> None:
    assert router().route("dry eyes", llm_hint="RETINA").kind == "clarify"


def test_no_concern_no_routing() -> None:
    o = router().route("book me with someone")
    assert o.kind == "none" and o.sentence is None and o.pending is None


def test_missing_doctor_falls_back_to_general_template() -> None:
    o = SpecialistRouter(lambda code: None, LEX).route("dry eyes")
    assert o.sentence == scripts.ROUTING_TEMPLATES["general"]


def test_lexicon_validation() -> None:
    with pytest.raises(ValueError, match="unknown specialty"):
        parse_lexicon(
            {"specialties": {"DENTAL": {"concern": "x", "clarify": "y", "phrases": ["z"]}}}
        )
    with pytest.raises(ValueError, match="no phrases"):
        parse_lexicon({"specialties": {"CORNEA": {"concern": "x", "clarify": "y", "phrases": []}}})
    assert isinstance(LEX, Lexicon) and LEX.version == 1
    assert [e.code for e in LEX.entries] == ["CORNEA", "RETINA", "GLAUCOMA", "PEDIATRIC", "GENERAL"]
