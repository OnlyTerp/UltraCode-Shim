"""Cost estimation and normalization across providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CostEstimate:
    input_cost: float
    output_cost: float
    request_cost: float
    total: float


class CostNormalizer:
    """Normalize provider-specific cost into a comparable unit.

    For v1 all costs are USD-equivalent. Future versions may add credit-type
    conversion tables (ACU, OR credits, Devin session credits).
    """

    def estimate(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cost_per_input: float = 0.0,
        cost_per_output: float = 0.0,
        cost_per_request: float = 0.0,
    ) -> CostEstimate:
        input_cost = prompt_tokens * cost_per_input
        output_cost = completion_tokens * cost_per_output
        return CostEstimate(
            input_cost=input_cost,
            output_cost=output_cost,
            request_cost=cost_per_request,
            total=input_cost + output_cost + cost_per_request,
        )

    def compare(self, a: CostEstimate, b: CostEstimate) -> float:
        """Return a - b; negative means a is cheaper."""
        return a.total - b.total
