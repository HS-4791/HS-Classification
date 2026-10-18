from __future__ import annotations

from dataclasses import dataclass, field


class CostCeilingExceeded(RuntimeError):
    """A single request tried to spend more than its ceiling allows."""

    def __init__(self, spent: float, ceiling: float) -> None:
        super().__init__(f"request cost ${spent:.4f} exceeds ceiling ${ceiling:.4f}")
        self.spent = spent
        self.ceiling = ceiling


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input_: float
    output: float
    cache_read: float | None = None


# Anthropic first-party API rates. Haiku 5.5 is priced for prompts up to 100K
# tokens; beyond that it is $0.50 / $2.50. Batch API runs at 50%.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
    "claude-opus-5": Price(5.00, 25.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20),
    "claude-haiku-5-5": Price(0.10, 0.50),
    # Local embedding runs on CPU in-process: no marginal token cost.
    "local-embedding": Price(0.0, 0.0),
}


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def cost_usd(self) -> float:
        try:
            price = PRICES[self.model]
        except KeyError as exc:
            raise KeyError(
                f"no price for model {self.model!r}; add it to hsclass.obs.cost.PRICES"
            ) from exc
        cache_rate = price.cache_read if price.cache_read is not None else price.input_
        return (
            self.input_tokens * price.input_
            + self.output_tokens * price.output
            + self.cache_read_tokens * cache_rate
        ) / 1_000_000


@dataclass
class CostLedger:
    """Accumulates spend for one request and enforces the ceiling."""

    ceiling_usd: float | None = None
    entries: list[Usage] = field(default_factory=list)

    def record(self, usage: Usage) -> float:
        self.entries.append(usage)
        total = self.total_usd
        if self.ceiling_usd is not None and total > self.ceiling_usd:
            raise CostCeilingExceeded(total, self.ceiling_usd)
        return total

    @property
    def total_usd(self) -> float:
        return sum(u.cost_usd() for u in self.entries)

    @property
    def total_tokens(self) -> int:
        return sum(u.input_tokens + u.output_tokens + u.cache_read_tokens for u in self.entries)

    def summary(self) -> dict:
        return {
            "total_usd": round(self.total_usd, 6),
            "total_tokens": self.total_tokens,
            "ceiling_usd": self.ceiling_usd,
            "calls": [
                {
                    "model": u.model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_tokens": u.cache_read_tokens,
                    "usd": round(u.cost_usd(), 6),
                }
                for u in self.entries
            ],
        }


def cost_per_1000(total_usd: float, n_requests: int) -> float:
    """The headline cost number the spec asks for."""
    if n_requests <= 0:
        return 0.0
    return total_usd / n_requests * 1000
