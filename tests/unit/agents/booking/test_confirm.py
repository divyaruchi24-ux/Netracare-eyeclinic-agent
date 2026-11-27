import pytest

from netracare.agents.booking.confirm import is_explicit_yes, is_no, is_stop

YES = [
    "yes",
    "Yes!",
    "YES PLEASE",
    "yep",
    "yeah",
    "Sure.",
    "ok",
    "okay, book it",
    "Book it",
    "go ahead",
    "Confirm",
    "yes, that's right",
    "Yes please book",
    "perfect, thanks",
    "haan",
    "haan ji",
    "theek hai",
    "book kar do",
    "👍 yes",
    "yes please, thank you",
]
NOT_YES = [
    "yes but make it 11",
    "yes, at 4pm?",
    "yes?",
    "ok maybe",
    "hmm yes",
    "not sure",
    "yes but on friday",
    "yes, or actually tuesday",
    "no",
    "nope",
    "yes no",
    "wait",
    "can you change it to the morning",
    "yes for tomorrow",
    "book it for my son instead",
    "I don't know",
    "sounds good but different doctor",
    "never mind",
    "yes if possible",
    "what time is that?",
    "",
    "  ",
    "yes 10:30",
    "nahi",
    "ok, cancel",
]


@pytest.mark.parametrize("text", YES)
def test_explicit_yes(text: str) -> None:
    assert is_explicit_yes(text)


@pytest.mark.parametrize("text", NOT_YES)
def test_not_an_explicit_yes(text: str) -> None:
    assert not is_explicit_yes(text)


@pytest.mark.parametrize(
    "text",
    [
        "never mind",
        "Forget it",
        "leave it",
        "no thanks",
        "stop",
        "don't book",
        "rehne do",
        "not now",
        "quit",
    ],
)
def test_stop(text: str) -> None:
    assert is_stop(text)


@pytest.mark.parametrize("text", ["cancel my appointment", "yes", "book next tuesday"])
def test_not_stop(text: str) -> None:
    assert not is_stop(text)


def test_no() -> None:
    assert is_no("no") and is_no("Nope, different time") and is_no("nahi")
    assert not is_no("yes") and not is_no("book on monday")
