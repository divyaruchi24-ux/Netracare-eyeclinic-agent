"""Eval datasets: one JSON object per line (`EvalCase`)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DATASETS_DIR = Path("evals/datasets")


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    input: dict[str, Any]
    expected: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    policy_ref: str | None = None


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class Dataset:
    name: str
    version: str  # content hash: any edit to the file changes it
    cases: tuple[EvalCase, ...]

    def __len__(self) -> int:
        return len(self.cases)


def load_dataset(path: Path) -> Dataset:
    raw = path.read_bytes()
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = EvalCase.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValueError) as exc:
            raise DatasetError(f"{path.name}:{lineno}: {exc}") from exc
        if case.id in seen:
            raise DatasetError(f"{path.name}:{lineno}: duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise DatasetError(f"{path.name}: dataset is empty")
    version = hashlib.sha256(raw).hexdigest()[:12]
    return Dataset(name=path.stem, version=version, cases=tuple(cases))


def dataset_path(name: str, root: Path = Path()) -> Path:
    return root / DATASETS_DIR / f"{name}.jsonl"
