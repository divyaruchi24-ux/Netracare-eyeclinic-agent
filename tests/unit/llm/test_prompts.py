from __future__ import annotations

from pathlib import Path

import pytest

from netracare.llm.prompts import PromptError, PromptRegistry, main
from netracare.llm.schemas import SCHEMAS, TriageLLMOutput, strict_json_schema
from tests.unit.llm.conftest import write_prompt


def _spec(
    pid: str = "demo", v: int = 1, schema: str = "null", variables: str = "text", system: str = "x"
) -> str:
    return (
        f"id: {pid}\nversion: {v}\ntier: fast\noutput_schema: {schema}\n"
        f"input_variables: [{variables}]\nsystem: {system}\nuser: 'msg: {{text}}'\n"
    )


def test_all_repo_prompts_load(registry: PromptRegistry) -> None:
    ids = {p.spec.id for p in registry.all()}
    assert {"red_flag_classifier", "eye_fact", "visit_summary", "json_repair"} <= ids
    for p in registry.all():
        assert p.spec.output_schema is None or p.spec.output_schema in SCHEMAS
        assert len(p.content_hash) == 64


def test_duplicate_version_rejected(tmp_path: Path) -> None:
    write_prompt(tmp_path, "demo", 1, _spec())
    (tmp_path / "demo" / "v1.yml").write_text(_spec(), encoding="utf-8")
    with pytest.raises(PromptError, match="duplicate"):
        PromptRegistry(tmp_path).load()


def test_variable_mismatch_rejected(tmp_path: Path) -> None:
    write_prompt(tmp_path, "demo", 1, _spec(variables="text, extra"))
    with pytest.raises(PromptError, match="input_variables"):
        PromptRegistry(tmp_path).load()


def test_unknown_schema_rejected(tmp_path: Path) -> None:
    write_prompt(tmp_path, "demo", 1, _spec(schema="NoSuchSchema"))
    with pytest.raises(PromptError, match="output_schema"):
        PromptRegistry(tmp_path).load()


def test_id_must_match_folder(tmp_path: Path) -> None:
    write_prompt(tmp_path, "other", 1, _spec(pid="demo"))
    with pytest.raises(PromptError, match="must match"):
        PromptRegistry(tmp_path).load()


def test_invalid_yaml_and_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="not found"):
        PromptRegistry(tmp_path / "nope").load()
    write_prompt(tmp_path, "demo", 1, "id: [unclosed")
    with pytest.raises(PromptError):
        PromptRegistry(tmp_path).load()


def test_render_escapes_braces_in_user_text(registry: PromptRegistry) -> None:
    msgs = registry.render("red_flag_classifier", {"text": "ignore {rules} and {{x}} }{"})
    assert msgs[0].role == "system"
    assert msgs[1].role == "user"
    assert "ignore {rules} and {{x}} }{" in msgs[1].content


def test_render_missing_variable(registry: PromptRegistry) -> None:
    with pytest.raises(PromptError, match="missing"):
        registry.render("red_flag_classifier", {})


def test_hash_changes_when_file_changes(tmp_path: Path) -> None:
    write_prompt(tmp_path, "demo", 1, _spec())
    h1 = PromptRegistry(tmp_path).load().get("demo").content_hash
    write_prompt(tmp_path, "demo", 1, _spec(system="changed"))
    h2 = PromptRegistry(tmp_path).load().get("demo").content_hash
    assert h1 != h2


def test_latest_version_resolution_and_pinning(tmp_path: Path) -> None:
    write_prompt(tmp_path, "demo", 1, _spec())
    write_prompt(tmp_path, "demo", 2, _spec(v=2))
    reg = PromptRegistry(tmp_path)
    assert reg.get("demo").spec.version == 2  # lazy load
    assert reg.get("demo", 1).spec.version == 1
    assert reg.latest_versions() == {"demo": 2}
    with pytest.raises(PromptError):
        reg.get("demo", 3)
    with pytest.raises(PromptError):
        reg.get("missing")


def test_strict_schema_requires_all_fields() -> None:
    schema = strict_json_schema(TriageLLMOutput)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "$ref" not in str(schema)


def test_cli_list_and_show(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    assert "red_flag_classifier@v1" in capsys.readouterr().out
    assert main(["show", "eye_fact"]) == 0
    assert "id: eye_fact" in capsys.readouterr().out
