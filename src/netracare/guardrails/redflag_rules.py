"""Deterministic red-flag rules for safety policy section 4 (E1-E10, U1-U6).

This layer is always on, costs nothing, and is tuned for **recall**: over-triggering is
acceptable (staff review alerts), missing an emergency is not. The LLM classifier can only
make a result more severe than these rules.

Pipeline: `normalize` (lowercase, straight quotes, can't/cant -> cannot, common typos) ->
*features* (regexes; a feature hit is ignored when a negation word - no/not/without/never -
appears within the 3 tokens before it) -> *codes* (boolean combinations of features).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from netracare.domain.enums import RedFlagLevel

_TYPOS: dict[str, str] = {
    "curtian": "curtain",
    "curtin": "curtain",
    "curtan": "curtain",
    "flashs": "flashes",
    "flahes": "flashes",
    "flashses": "flashes",
    "floatrs": "floaters",
    "floters": "floaters",
    "flotters": "floaters",
    "vison": "vision",
    "vission": "vision",
    "visoin": "vision",
    "bleech": "bleach",
    "painfull": "painful",
    "swolen": "swollen",
    "sudenly": "suddenly",
    "suddently": "suddenly",
    "suddnly": "suddenly",
    "blury": "blurry",
    "doubel": "double",
    "eys": "eyes",
    "ey": "eye",
    "vomitting": "vomiting",
    "feaver": "fever",
}
_NEGATIONS = frozenset({"no", "not", "without", "never", "dont", "nor", "neither", "zero"})
_NEG_WINDOW = 3
# A negation does not reach across a clause boundary ("no flashes, just floaters").
_CLAUSE_BREAK = re.compile(r"[,.;:!?()]|\b(?:but|just|and|though|although|however|only|except)\b")

CHILD = r"\b(child|children|kid|kids|son|daughter|baby|babies|toddler|infant|newborn|new born|boy|girl|grandson|granddaughter|\d{1,2}[ -]?(year|yr|month)s?[ -]?old)('?s)?\b"


def normalize(text: str) -> str:
    t = text.lower()
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"\b(can't|cant|can not|cannot|couldn't|couldnt|could not)\b", "cannot", t)
    t = re.sub(
        r"\b(don't|do not|doesn't|does not|didn't|did not|isn't|is not|haven't)\b", "dont", t
    )
    t = re.sub(r"\b(\w+)\b", lambda m: _TYPOS.get(m.group(1), m.group(1)), t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass(frozen=True)
class RuleMatch:
    code: str
    level: RedFlagLevel
    span: str


# --- features -------------------------------------------------------------------------
_F: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern)
    for name, pattern in {
        "sudden": r"\b(sudden|suddenly|all of a sudden|abrupt|abruptly|out of nowhere|just now)\b",
        "vision_loss": (
            r"\b(cannot see (anything|at all|a thing|out of|from|with|properly from)"
            r"|lost (my |the |all |most )?(vision|sight)|loss of (vision|sight)|vision loss"
            r"|(vision|sight|eye|everything|screen|world) (has |had )?(just )?(went|go|goes|going|turned|gone|became|become|is going|faded|fading) (black|dark|blank|blind|out)"
            r"|went blind|gone blind|going blind|blacked out|black ?out in (my |one |the )?(eye|vision)"
            r"|half (of )?my vision (is )?(gone|missing|black|dark)|blind in (one|my|the|left|right))"
        ),
        "curtain": (
            r"\b(curtain|veil)\b|\b(grey|gray|black|dark) (shadow|patch|area|cloud)\b"
            r"|\bshadow (on|over|across|in|coming|covering|at)\b"
        ),
        "flashes": r"\b(flash|flashes|flashing|flashers|sparks|lightning|streaks of light|flickering lights?)\b",
        "floaters": r"\b(floaters?|floating (spots|specks|dots|things)|shower of (spots|dots|specks)|cobwebs?|black (spots|dots|specks))\b",
        "chemical": (
            r"\b(bleach|acid|alkali|cement|lime|chuna|chemical|chemicals|battery (fluid|acid|water)"
            r"|ammonia|detergent|drain cleaner|toilet cleaner|harpic|phenyl|phenol|kerosene|petrol"
            r"|diesel|pepper spray|super ?glue|glue|lye|caustic|solvent|paint thinner|pesticide)\b"
        ),
        "splash": r"\b(splash|splashed|splashing|sprayed|squirted|went (in|into)|got (in|into)|in (my|his|her|their|the|our|your|one) (\w+'s |\w+ )?eyes?)\b",
        "eye": r"\b(eye|eyes|eyeball|eyelid|eyelids|vision|sight|cornea|pupil)\b",
        "trauma": (
            r"\b(hit|struck|smacked|whacked) (in|on|near) (the |my |his |her |their |one )?(eye|face)"
            r"|\bhit by (a |the )?\w*\s?(ball|stick|bat|fist|punch|stone|rock|elbow|racket|shuttle)"
            r"|\b(punched|stabbed|poked|scratched) (in |on )?(the |my |his |her )?(eye|eyeball)"
            r"|\b(metal|glass|wood|wooden|piece|particle|splinter|wire|thorn|sand|stone|chip|shard|fragment|object|something)\b.{0,30}\b(stuck|flew|flying|lodged|embedded|poked|went in|went into|into (my|his|her|the) eye|in (my|his|her|the) eye)"
            r"|\b(eye|eyeball) (injury|injured|trauma|cut|wound|bleeding)|\binjur(y|ed|ies) (to |in |on )?(the |my |his |her )?eye"
            r"|\b(firecracker|cracker|fireworks?) (burst|exploded|hit|went)"
        ),
        "severe_pain": (
            r"\b(severe|severely|bad|terrible|intense|extreme|excruciating|unbearable|worst|really bad|very bad|very painful|extremely painful|so painful|killing)\b"
            r"(.{0,20}\b(pain|ache|aching|hurts?|hurting|painful)\b)?"
        ),
        "pain": r"\b(pain|painful|ache|aching|aches|hurts?|hurting|sore|throbbing|stinging|burning)\b",
        "e6_assoc": r"\b(vomit|vomiting|throwing up|threw up|nausea|nauseous|feel sick|headache|halos?|rainbow|rings around (the )?lights?|circles around lights?)\b",
        "double": r"\b(double vision|seeing double|seeing two|see double|see two of everything|two images)\b",
        "neuro": r"\b(numb|numbness|weak|weakness|slurred|slurring|droop|drooping|droopy|face (is )?(drooping|numb|weak)|cannot speak|confus)\w*",
        "droop": r"\b(droop|drooping|droopy)\w* (eye ?lid|eyelid|lid|face)|\b(eye ?lid|eyelid) (is |has )?(droop|drooping|fallen|dropped)",
        "surgery": r"\b(surgery|surgeries|operation|operated|op|post[- ]?op|injection|injections|laser|lasik|cataract|procedure|intravitreal)\b",
        "post_op": r"\b(pain|painful|red|redness|discharge|pus|blurr|blurry|blurred|worse|worsening|cannot see|vision (is )?(dropping|gone|bad)|swollen|swelling)\w*",
        "lens": r"\b(contact lens|contact lenses|contacts|lens wearer|lenses|wear lenses|wear contacts)\b",
        "photophobia": r"\b(light|lights|bright|sunlight|photophobia|cannot open|cannot keep (it|my eye) open|red|redness)\b",
        "bulging": r"\b(bulging|bulge|bulges|protruding|protrudes|popping out|pushed out|sticking out)\b",
        "swelling": r"\b(swollen|swelling|swelled|puffy|puffed)\b",
        "fever": r"\b(fever|feverish|temperature|high temp)\b",
        "red": r"\b(red|redness|pink|bloodshot)\b",
        "gradual": (
            r"\b(gradual|gradually|slowly|steadily|over (the )?(last|past) (few )?(days|weeks|week|month|months)|for (a few |some )?(days|weeks)|getting worse|got worse|been getting|keeps getting|worse and worse|day by day)\b"
        ),
        "vision_worse": r"\b(vision|sight|seeing|see|eyesight)\b.{0,40}\b(worse|blurr|blurry|blurred|dim|dimmer|hazy|foggy|cloudy|weaker|declin)\w*|\b(worse|blurry|blurred|hazy|dim) (vision|sight|eyesight)\b",
        "white_pupil": r"\b(white|glowing|glow|yellow|cat'?s eye|shiny) (pupil|reflex|reflection|spot in (the )?pupil)|\bpupil\b.{0,30}\b(white|glow|glowing|yellow|shiny)\b",
        "turned_eye": r"\b(turned|turning|turns|crossed|cross|crossing|squint|squinting|wandering|lazy|drifting|drifts) (in|out|inward|inwards|outward|outwards|eye|eyes)\b|\b(eye|eyes)\b.{0,25}\b(turned|turning|turns|crossing|drifting|drifts|wanders|wandering)\b|\bsquint\b",
        "discharge": r"\b(discharge|sticky|pus|goo|gunk|crusty|crust|oozing|mucus)\b",
        "newborn": r"\b(newborn|new born|new-born|infant|neonate|baby|babies|\d{1,2}[ -]?(week|weeks|month|months|day|days)[ -]?old)\b",
        "ran_out": r"\b(ran out|run out|running out|out of|finished|no more|used up|forgot to (buy|refill)|cannot get|unable to get|missed)\b.{0,30}\b(drops|medicine|medication|meds)\b",
        "glaucoma_drops": r"\b(glaucoma|pressure|timolol|latanoprost|travatan|xalatan|lumigan|prescribed|eye) (eye )?(drops|medicine|medication|meds)\b|\bglaucoma\b",
        "child": CHILD,
    }.items()
}

_CLINICAL = re.compile(
    r"\b(eye|eyes|eyeball|eyelid|vision|sight|see|seeing|blur|blurry|blurred|glasses|spectacles|specs"
    r"|lens|lenses|contacts|drops|pressure|floaters?|itch|itchy|itching|watery|watering|tears|dry"
    r"|red|pain|painful|sore|swollen|cataract|glaucoma|retina|squint|check ?-?up|eye test|surgery"
    r"|dose|medicine|medication|tablet|prescription|symptom|symptoms|headache|allergy)\b"
)


def _hits(name: str, text: str) -> list[str]:
    """Non-negated matches of a feature."""
    out: list[str] = []
    for m in _F[name].finditer(text):
        clause = _CLAUSE_BREAK.split(text[: m.start()])[-1]
        before = re.findall(r"[a-z']+", clause)[-_NEG_WINDOW:]
        if any(tok in _NEGATIONS for tok in before):
            continue
        out.append(m.group(0))
    return out


def detect(text: str) -> list[RuleMatch]:
    """All red-flag codes found anywhere in the message (hidden-emergency rule, 4.3)."""
    t = normalize(text)
    h: dict[str, list[str]] = {name: _hits(name, t) for name in _F}
    found: list[RuleMatch] = []

    def add(code: str, *spans: str) -> None:
        level = RedFlagLevel.EMERGENCY if code.startswith("E") else RedFlagLevel.URGENT
        span = next((s for s in spans if s), "")
        found.append(RuleMatch(code=code, level=level, span=span[:60]))

    def first(*names: str) -> str:
        for n in names:
            if h[n]:
                return h[n][0]
        return ""

    if h["vision_loss"]:
        add("E1", first("vision_loss"))
    if h["curtain"]:
        add("E2", first("curtain"))
    if h["flashes"] and h["floaters"]:
        add("E3", first("flashes"))
    elif (
        re.search(
            r"\bshower of\b|\b(lots|loads|hundreds|tons|many|so many) of (new )?(floaters|spots|dots)\b",
            t,
        )
        and h["floaters"]
    ):
        add("E3", first("floaters"))
    if h["chemical"] and (h["splash"] or h["eye"]):
        add("E4", first("chemical"))
    if h["trauma"]:
        add("E5", first("trauma"))
    if (h["severe_pain"] or h["pain"]) and h["e6_assoc"] and (h["severe_pain"] or h["eye"]):
        add("E6", first("e6_assoc"))
    # E7 is about *sudden* drooping or nerve signs (policy 4.1). A long-standing droopy lid is a
    # routine ptosis consult (kb/procedures/ptosis.md), so a droop alone no longer escalates;
    # with "sudden", a nerve sign (numbness, weakness, slurring) or a headache it still does.
    nerve = [x for x in h["neuro"] if not x.startswith("droop")]
    droop_red_flag = h["droop"] and (h["sudden"] or nerve or "headache" in h["e6_assoc"])
    if h["double"] or droop_red_flag or (nerve and h["eye"] and h["sudden"]):
        add("E7", first("double", "droop", "neuro"))
    if h["surgery"] and h["post_op"] and not _is_pre_op_question(t):
        add("E8", first("surgery"))
    if h["lens"] and (h["pain"] or h["severe_pain"]) and h["photophobia"]:
        add("E9", first("lens"))
    if (h["bulging"] and h["eye"]) or (h["swelling"] and h["fever"]):
        add("E10", first("bulging", "swelling"))

    if h["floaters"] and not any(m.code == "E3" for m in found):
        add("U1", first("floaters"))
    if h["red"] and h["pain"] and h["eye"]:
        add("U2", first("red"))
    if h["vision_worse"] and h["gradual"]:
        add("U3", first("vision_worse"))
    if h["white_pupil"] or (h["turned_eye"] and h["child"]):
        add("U4", first("white_pupil", "turned_eye"))
    if h["newborn"] and h["discharge"]:
        add("U5", first("discharge"))
    if h["ran_out"] and h["glaucoma_drops"]:
        add("U6", first("ran_out"))
    return found


def _is_pre_op_question(t: str) -> bool:
    """'before the surgery' questions are about preparation, not post-op symptoms."""
    return bool(
        re.search(r"\bbefore (the |my |a )?(surgery|operation|procedure|lasik|laser)\b", t)
    ) and not re.search(r"\b(after|since|ago|post)\b", t)


def level_for(matches: Iterable[RuleMatch]) -> RedFlagLevel | None:
    levels = [m.level for m in matches]
    return RedFlagLevel.most_severe(*levels) if levels else None


def looks_clinical(text: str) -> bool:
    return bool(_CLINICAL.search(normalize(text)))


def mentions_child(text: str) -> bool:
    return bool(_hits("child", normalize(text)))


_CODE_SPECIALTY: dict[str, str] = {
    "E2": "RETINA",
    "E3": "RETINA",
    "U1": "RETINA",
    "E6": "GLAUCOMA",
    "U6": "GLAUCOMA",
    "E9": "CORNEA",
    "U4": "PEDIATRIC",
    "U5": "PEDIATRIC",
}

# Neutral concern phrases per code (used in routing sentences instead of LLM text).
CODE_CONCERNS: dict[str, str] = {
    "U1": "new floaters",
    "U2": "a red, painful eye",
    "U3": "vision that is getting worse",
    "U4": "a child's eye appearance",
    "U5": "a baby's eye discharge",
    "U6": "running out of prescribed eye drops",
}


def specialty_for(codes: Iterable[str], text: str) -> str | None:
    """Rule-derived specialty, or None when the rules have no opinion."""
    code_list = list(codes)
    if mentions_child(text) or any(c in ("U4", "U5") for c in code_list):
        return "PEDIATRIC"
    for c in code_list:
        if c in _CODE_SPECIALTY:
            return _CODE_SPECIALTY[c]
    return "GENERAL" if code_list else None


_MEDS = r"(drops?|eye ?drops|medicine|medicines|medication|medications|meds|tablets?|pills?|treatment|insulin|steroids?|antibiotics?|ointment)"
_MEDICAL_QUESTION = re.compile(
    r"\b(what|which|how much|how many) (dose|dosage|drops?|medicine|medication|tablets?|eye ?drops)\b"
    r"|\bdose of\b"
    r"|\bshould i (stop|start|take|use|skip|change|continue|keep using|put)\b.{0,40}\b"
    + _MEDS
    + r"\b"
    r"|\bcan i (stop|take|use|skip|put)\b.{0,40}\b(drops?|medicine|medication|tablets?|lenses)\b"
    r"|\bis (that|it|this) (dangerous|normal|serious|bad|ok|okay|safe)\b"
    r"|\bwhat (does|do) (my|the|this|these) .{0,40}\bmean\b|\bdo i have\b"
    r"|\bdo i need (a |an |any )?(surgery|operation|glasses|specs|spectacles|drops|eye ?drops|medicine|medication|treatment|laser|lasik|tablets?|antibiotics?)\b"
    r"|\bwhat (is wrong|could it be|might it be)\b|\bdiagnos\w*\b"
)
# Preparation questions answered by the KB ("when should I stop wearing lenses before the LASIK
# evaluation?") are logistics, not medical advice, unless they mention a medicine.
_PREP_CONTEXT = re.compile(
    r"\bbefore (the |my |a |an )?(evaluation|assessment|surgery|operation|visit|exam|examination"
    r"|appointment|test|scan|consultation|procedure|lasik|check ?-?up)\b"
)
_MEDS_RE = re.compile(r"\b" + _MEDS + r"\b")


def asks_medical_advice(text: str) -> bool:
    """A request for medical advice, interpretation or a diagnosis (answered with the
    section 5.3 refusal script, never with advice)."""
    t = normalize(text)
    if _PREP_CONTEXT.search(t) and not _MEDS_RE.search(t):
        return False
    return bool(_MEDICAL_QUESTION.search(t))
