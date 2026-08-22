"""Routing engine: the main select_route entry point."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..failover.health import HealthRegistry
from ..ledger.models import Ledger
from ..providers.registry import RouteRegistry
from ..telemetry.collector import TelemetryCollector
from .decision import RoutingDecision
from .policy import SelectionPolicy
from .task_tiers import TaskTierDetector


@dataclass
class RoutingEngine:
    """Selects a route for each request based on ledger, capability, and health."""

    ledger: Ledger
    registry: RouteRegistry
    health: HealthRegistry
    telemetry: TelemetryCollector
    detector: TaskTierDetector = field(default_factory=TaskTierDetector)
    policy: SelectionPolicy = field(
        default_factory=lambda: SelectionPolicy(
            tier_thresholds={
                "planning": 0.80,
                "heavy_reasoning": 0.90,
                "bulk_context": 0.60,
                "frontend": 0.70,
            }
        )
    )
    max_failover_depth: int = 3

    def select_route(
        self,
        prompt: str,
        prompt_tokens: int = 0,
        max_tokens: int = 4096,
        has_images: bool = False,
        has_tools: bool = False,
        explicit_tier: Optional[str] = None,
    ) -> RoutingDecision:
        """Return a routing decision for a single request.

        This is a scaffold: the cost/health/failover logic is wired but the
        provider dispatch path is intentionally not implemented.
        """
        request_id = str(uuid.uuid4())
        tier = self.detector.detect(
            prompt, has_images, 0, prompt_tokens, explicit_tier
        ).value

        candidates: List[Any] = []
        for route in self.registry.routes.values():
            account = self.ledger.account(route.account_id)
            if account is None:
                continue
            if not self.policy.is_eligible(
                route, account, tier, prompt_tokens, max_tokens, has_images, has_tools
            ):
                continue
            if self.health.is_unhealthy(route.route_id):
                continue
            candidates.append(
                (
                    route,
                    account,
                    self.policy.sort_key(
                        route, account, tier, prompt_tokens, max_tokens
                    ),
                )
            )

        if not candidates:
            return RoutingDecision(
                request_id=request_id,
                route_id=None,
                tier=tier,
                outcome="no_eligible_route",
                reason="No route satisfied capability, quota, and health constraints.",
            )

        candidates.sort(key=lambda x: x[2])
        fallback_chain = [c[0].route_id for c in candidates[: self.max_failover_depth]]
        chosen = candidates[0]

        return RoutingDecision(
            request_id=request_id,
            route_id=chosen[0].route_id,
            tier=tier,
            estimated_cost=self.policy.effective_cost(
                chosen[0], chosen[1], prompt_tokens, max_tokens
            ),
            fallback_chain=fallback_chain,
            scores=chosen[0].capability.scores if chosen[0].capability else {},
            reason=f"local={chosen[2][0]==0}; score={-chosen[2][1]:.2f}; cost={chosen[2][2]:.6f}",
        )
