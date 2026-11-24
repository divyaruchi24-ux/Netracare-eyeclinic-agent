import pytest

from netracare.rag.doctor_names import DoctorName, match_doctor

DOCTORS = [
    DoctorName("D1", "Dr. Ananya Rao"),
    DoctorName("D2", "Dr. Vikram Shetty"),
    DoctorName("D3", "Dr. Meera Iyer"),
    DoctorName("D4", "Dr. Arjun Menon"),
    DoctorName("D5", "Dr. Kavya Nair"),
]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("Dr Shetti timings", "D2"),
        ("vikram shety", "D2"),
        ("is dr. meera available on monday", "D3"),
        ("dr iyar", "D3"),
        ("doctor menen", "D4"),
        ("Dr. Nayar", "D5"),
        ("kavya", "D5"),
        ("rao", "D1"),
        ("dr. rau", "D1"),
        ("ananiya", "D1"),
    ],
)
def test_matches(query: str, code: str) -> None:
    m = match_doctor(query, DOCTORS)
    assert m is not None and m.code == code


@pytest.mark.parametrize(
    "query",
    [
        "dry eye",
        "red eye",
        "the eye doctor",
        "who is the cataract doctor",
        "retina doctor",
        "are you open on sunday",
        "Iyar",  # loose matches only right after "Dr"
        "",
    ],
)
def test_no_false_positives(query: str) -> None:
    assert match_doctor(query, DOCTORS) is None


def test_exact_match_scores_one() -> None:
    m = match_doctor("Dr. Vikram Shetty", DOCTORS)
    assert m is not None and m.score == 1.0
