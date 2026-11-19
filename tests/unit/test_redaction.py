from netracare.log import redact_all, redact_names


def test_redact_names_full_and_parts_case_insensitive() -> None:
    text = "ASHA Verma said asha's eye hurts; Vermas is not a match"
    out = redact_names(text, ["Asha Verma"])
    assert "asha" not in out.lower().replace("vermas", "")
    assert out.startswith("the patient said the patient's eye hurts")
    assert "Vermas" in out  # word boundary respected


def test_redact_names_ignores_short_and_empty_parts() -> None:
    assert redact_names("Al went to the clinic", ["Al B", ""]) == "Al went to the clinic"
    assert redact_names("Al B went", ["Al B"]) == "the patient went"


def test_redact_all_combines_names_email_phone() -> None:
    out = redact_all("Ravi at ravi@example.com or +91 98450 12345", ["Ravi Kumar"])
    assert out == "the patient at [EMAIL] or [PHONE]"
    assert redact_all("plain", ()) == "plain"
