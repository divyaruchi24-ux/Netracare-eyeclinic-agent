"""Model pricing (USD per 1M tokens) from `config/model_pricing.yaml`."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from netracare.log import get_logger

log = get_logger(__name__)
_MILLION = Decimal(1_000_000)
_Q = Decimal("0.000001")


@dataclass(frozen=True)
class ModelPrice:
    input_per_m: Decimal
    output_per_m: Decimal


@dataclass(frozen=True)
class Pricing:
    models: dict[str, ModelPrice] = field(default_factory=dict)
    verified_on: str | None = None
    source_url: str | None = None

    @classmethod
    def load(cls, path: Path | str) -> Pricing:
        p = Path(path)
        if not p.is_file():
            log.warning("llm.pricing_missing", path=str(p))
            return cls()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        models = {
            name: ModelPrice(
                input_per_m=Decimal(str(v["input_per_m"])),
                output_per_m=Decimal(str(v["output_per_m"])),
            )
            for name, v in (data.get("models") or {}).items()
        }
        return cls(
            models=models, verified_on=data.get("verified_on"), source_url=data.get("source_url")
        )

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        price = self.models.get(model)
        if price is None:
            log.warning("llm.unknown_model_price", model=model)
            return Decimal(0)
        raw = (price.input_per_m * input_tokens + price.output_per_m * output_tokens) / _MILLION
        return raw.quantize(_Q)

    def estimate(self, model: str, input_chars: int, max_output_tokens: int) -> Decimal:
        """Worst-case estimate used by the budget check (~4 chars per token)."""
        return self.cost(model, input_chars // 4 + 1, max_output_tokens)
