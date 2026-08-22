"""HTTP endpoint handlers for Life OS metrics.

These are intended to be mounted into `proxy.py` under `/life-os/*` paths when
`UC_ROUTING_ENGINE=1` and `routing_engine.life_os.enabled` are true.
"""

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict

from .metrics import RoutingMetrics


class MetricsHandler:
    """Serve routing metrics as JSON."""

    def __init__(self, metrics: RoutingMetrics) -> None:
        self.metrics = metrics

    def get_routing(self) -> Dict[str, Any]:
        return self.metrics.to_dict()

    def get_providers(self) -> Dict[str, Any]:
        return {
            "provider_health": self.metrics.provider_health,
            "cost_by_provider": self.metrics.cost_by_provider,
            "requests_by_provider": self.metrics.requests_by_provider,
        }

    def get_health(self) -> Dict[str, Any]:
        return {"provider_health": self.metrics.provider_health}

    def get_cost(self) -> Dict[str, Any]:
        return {
            "window": self.metrics.window,
            "total_estimated_cost": self.metrics.total_estimated_cost,
            "cost_by_provider": self.metrics.cost_by_provider,
        }
