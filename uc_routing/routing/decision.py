"""Routing decision record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RoutingDecision:
    """The outcome of a routing decision."""

    request_id: str
    route_id: Optional[str]
    tier: str
    estimated_cost: float = 0.0
    fallback_chain: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)
    outcome: str = "selected"  # selected | no_eligible_route | reservation_failed
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
