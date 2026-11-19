"""Versioned prompt registry: `prompts/<prompt_id>/v<N>.yaml`.

Each file is validated at load time: unique (id, version), declared `input_variables` equal
the variables the templates use, and `output_schema` (if any) is registered in `SCHEMAS`.
Templates use LangChain `ChatPromptTemplate` (f-string) syntax; variable *values* are never
parsed as templates, so braces in user text are safe.

CLI: `python -m netracare.llm.prompts {list,show <id> [--version N],sync-opik}`.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

from netracare.llm.schemas import SCHEMAS
from netracare.llm.types import ChatMessage

_FILE_RE = re.compile(r"^v(\d+)\.ya?ml$")


class PromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)
    tier: Literal["fast", "reasoning"]
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: int = Field(default=512, ge=16, le=8192)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    output_schema: str | None = None
    system: str
    user: str
    input_variables: list[str]
    changelog: str = ""
    owner: str = "clinic-admin"
    policy_version: str = "1.0"


@dataclass(frozen=True)
class LoadedPrompt:
    spec: PromptSpec
    content_hash: str
    path: Path

    @property
    def ref(self) -> str:
        return f"{self.spec.id}@v{self.spec.version}"

    def template(self) -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages(
            [("system", self.spec.system), ("human", self.spec.user)]
        )


class PromptError(ValueError):
    pass


class PromptRegistry:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._prompts: dict[str, dict[int, LoadedPrompt]] = {}
        self._loaded = False

    def load(self) -> PromptRegistry:
        prompts: dict[str, dict[int, LoadedPrompt]] = {}
        if not self.root.is_dir():
            raise PromptError(f"prompts directory not found: {self.root}")
        for path in sorted(self.root.glob("*/v*.y*ml")):
            match = _FILE_RE.match(path.name)
            if match is None:
                continue
            loaded = _load_file(path)
            spec = loaded.spec
            if spec.id != path.parent.name or spec.version != int(match.group(1)):
                raise PromptError(f"{path}: id/version must match the folder and file name")
            versions = prompts.setdefault(spec.id, {})
            if spec.version in versions:
                raise PromptError(f"duplicate prompt {spec.id} v{spec.version}")
            versions[spec.version] = loaded
        self._prompts = prompts
        self._loaded = True
        return self

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def get(self, prompt_id: str, version: int | None = None) -> LoadedPrompt:
        self._ensure()
        versions = self._prompts.get(prompt_id)
        if not versions:
            raise PromptError(f"unknown prompt {prompt_id!r}")
        if version is None:
            return versions[max(versions)]
        if version not in versions:
            raise PromptError(f"unknown version {prompt_id} v{version}")
        return versions[version]

    def render(
        self, prompt_id: str, variables: Mapping[str, Any], version: int | None = None
    ) -> list[ChatMessage]:
        return render(self.get(prompt_id, version), variables)

    def all(self) -> list[LoadedPrompt]:
        self._ensure()
        return [v[n] for v in self._prompts.values() for n in sorted(v)]

    def latest_versions(self) -> dict[str, int]:
        self._ensure()
        return {pid: max(v) for pid, v in sorted(self._prompts.items())}


def render(prompt: LoadedPrompt, variables: Mapping[str, Any]) -> list[ChatMessage]:
    declared = set(prompt.spec.input_variables)
    missing = declared - set(variables)
    if missing:
        raise PromptError(f"{prompt.ref}: missing variables {sorted(missing)}")
    values = {k: str(variables[k]) for k in declared}
    out: list[ChatMessage] = []
    for msg in prompt.template().format_messages(**values):
        role: Literal["system", "user"] = "system" if msg.type == "system" else "user"
        out.append(ChatMessage(role=role, content=str(msg.content)))
    return out


def _load_file(path: Path) -> LoadedPrompt:
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw)
        spec = PromptSpec.model_validate(data)
    except (yaml.YAMLError, ValueError) as exc:
        raise PromptError(f"{path}: {exc}") from exc
    if spec.output_schema is not None and spec.output_schema not in SCHEMAS:
        raise PromptError(f"{path}: unknown output_schema {spec.output_schema!r}")
    try:
        used = set(
            ChatPromptTemplate.from_messages(
                [("system", spec.system), ("human", spec.user)]
            ).input_variables
        )
    except (KeyError, ValueError) as exc:
        raise PromptError(f"{path}: bad template: {exc}") from exc
    if used != set(spec.input_variables):
        raise PromptError(
            f"{path}: input_variables {sorted(spec.input_variables)} != template {sorted(used)}"
        )
    return LoadedPrompt(spec=spec, content_hash=hashlib.sha256(raw).hexdigest(), path=path)


def sync_opik(registry: PromptRegistry) -> int:  # pragma: no cover - needs a live Opik key
    """Push every prompt to the Opik Prompt Library (versioned by content)."""
    import opik

    from netracare.config import get_settings
    from netracare.observability.opik_config import apply_opik_env

    apply_opik_env(get_settings())
    client = opik.Opik()
    for p in registry.all():
        client.create_prompt(
            name=f"netracare.{p.spec.id}",
            prompt=f"[system]\n{p.spec.system}\n[user]\n{p.spec.user}",
            metadata={"version": p.spec.version, "hash": p.content_hash, "tier": p.spec.tier},
        )
    return len(registry.all())


def main(argv: list[str] | None = None) -> int:
    from netracare.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m netracare.llm.prompts")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("prompt_id")
    show.add_argument("--version", type=int)
    sub.add_parser("sync-opik")
    args = parser.parse_args(argv)
    registry = PromptRegistry(get_settings().prompts_dir).load()
    if args.cmd == "list":
        for p in registry.all():
            schema = p.spec.output_schema
            print(f"{p.ref:32} tier={p.spec.tier:9} schema={schema} {p.content_hash[:12]}")
    elif args.cmd == "show":
        p = registry.get(args.prompt_id, args.version)
        print(p.path.read_text(encoding="utf-8"))
    else:  # pragma: no cover
        print(f"synced {sync_opik(registry)} prompts to Opik")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
