"""Selection policy, cost function, and failover ordering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..ledger.models import Account, EntitlementKind, Ledger
from ..providers.types import CapabilityProfile, Route
from ..telemetry.cost import CostNormalizer
from .decision import RoutingDecision


@dataclass
class SelectionPolicy:
    """Encapsulates the rules for choosing and ordering candidate routes."""

    tier_thresholds: Dict[str, float]
    cost_normalizer: CostNormalizer = CostNormalizer()

    def is_eligible(
        self,
        route: Route,
        account: Account,
        tier: str,
        prompt_tokens: int,
        max_tokens: int,
        has_images: bool,
        has_tools: bool,
    ) -> bool:
        """Return True if route satisfies capability and quota constraints."""
        if not route.enabled or not account.enabled:
            return False
        profile = route.capability
        if profile is None:
            return False
        score = profile.scores.get(tier, 0.0)
        if score < self.tier_thresholds.get(tier, 0.7):
            return False
        if prompt_tokens + max_tokens > profile.context_window:
            return False
        if has_images and not profile.supports_vision:
            return False
        if has_tools and not profile.supports_tools:
            return False
        return account.can_fulfill_request(prompt_tokens, max_tokens)

    def effective_cost(
        self,
        route: Route,
        account: Account,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Compute a comparable effective cost for sorting.

        Local accounts are zero monetary cost. Subscriptions carry an opportunity
        cost as their window fills up. Prepaid accounts are direct cost.
        """
        entitlement = self._primary_entitlement(account)
        cost_per_input = 0.0
        cost_per_output = 0.0
        cost_per_request = 0.0
        pressure = 0.0

        if entitlement:
            cost_per_input = entitlement.cost_per_input_token or 0.0
            cost_per_output = entitlement.cost_per_output_token or 0.0
            cost_per_request = entitlement.cost_per_request or 0.0
            if entitlement.limit > 0:
                pressure = entitlement.used / entitlement.limit

        estimate = self.cost_normalizer.estimate(
            prompt_tokens, completion_tokens, cost_per_input, cost_per_output, cost_per_request
        )
        if self._is_local(account):
            return estimate.total + (pressure * 0.001)  # tiny load penalty
        return estimate.total * (1.0 + pressure)

    def _primary_entitlement(self, account: Account) -> Optional[Any]:
        for ent in account.entitlements:
            return ent
        return None

    def _is_local(self, account: Account) -> bool:
        return account.kind.value == "local"

    def sort_key(
        self,
        route: Route,
        account: Account,
        tier: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> Tuple[int, float, float]:
        """Return a tuple for sorting candidates.

        Order: local first (0), then non-local (1); then capability score desc
        (negated), then effective cost asc.
        """
        score = route.capability.scores.get(tier, 0.0) if route.capability else 0.0
        cost = self.effective_cost(route, account, prompt_tokens, completion_tokens)
        local_order = 0 if self._is_local(account) else 1
        return (local_order, -score, cost)
