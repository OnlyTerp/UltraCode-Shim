"""Prepaid credit pool and local compute capacity accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Account, Entitlement, EntitlementKind


@dataclass
class BalanceSheet:
    """Tracks live balances for a prepaid or local account."""

    account_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    estimated_cost: float = 0.0

    def spend(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_per_input: float = 0.0,
        cost_per_output: float = 0.0,
        cost_per_request: float = 0.0,
    ) -> float:
        """Record a spend and return the estimated cost."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.requests += 1
        cost = (
            input_tokens * cost_per_input
            + output_tokens * cost_per_output
            + cost_per_request
        )
        self.estimated_cost += cost
        return cost

    def remaining_balance(self, entitlement: Entitlement) -> float:
        """Return remaining balance for a prepaid entitlement."""
        if entitlement.kind != EntitlementKind.PREPAID:
            return 0.0
        if entitlement.minimum_balance is None:
            return entitlement.limit - self.estimated_cost
        return (entitlement.limit - entitlement.minimum_balance) - self.estimated_cost
