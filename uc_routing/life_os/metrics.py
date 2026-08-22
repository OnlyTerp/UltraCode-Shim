"""Aggregated metrics for the Life OS dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RoutingMetrics:
    """Metrics payload consumed by terpOS / Life OS."""

    window: str = "24h"
    total_requests: int = 0
    requests_by_tier: Dict[str, int] = field(default_factory=dict)
    requests_by_provider: Dict[str, int] = field(default_factory=dict)
    total_estimated_cost: float = 0.0
    cost_by_provider: Dict[str, float] = field(default_factory=dict)
    avg_ttft_ms: Optional[float] = None
    p95_ttft_ms: Optional[float] = None
    avg_e2e_latency_ms: Optional[float] = None
    p95_e2e_latency_ms: Optional[float] = None
    quota_remaining: Dict[str, float] = field(default_factory=dict)
    provider_health: Dict[str, str] = field(default_factory=dict)
    active_cooldowns: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window": self.window,
            "total_requests": self.total_requests,
            "requests_by_tier": self.requests_by_tier,
            "requests_by_provider": self.requests_by_provider,
            "total_estimated_cost": self.total_estimated_cost,
            "cost_by_provider": self.cost_by_provider,
            "avg_ttft_ms": self.avg_ttft_ms,
            "p95_ttft_ms": self.p95_ttft_ms,
            "avg_e2e_latency_ms": self.avg_e2e_latency_ms,
            "p95_e2e_latency_ms": self.p95_e2e_latency_ms,
            "quota_remaining": self.quota_remaining,
            "provider_health": self.provider_health,
            "active_cooldowns": self.active_cooldowns,
        }
