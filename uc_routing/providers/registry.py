"""Route registry built from config.json."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .types import CapabilityProfile, ProviderType, Route


@dataclass
class RouteRegistry:
    """Index of configured routes."""

    routes: Dict[str, Route] = field(default_factory=dict)

    def add(self, route: Route) -> None:
        self.routes[route.route_id] = route

    def get(self, route_id: str) -> Optional[Route]:
        return self.routes.get(route_id)

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> RouteRegistry:
        """Build a registry from the `routing_engine` config section.

        This is a scaffold; the full loader lives in `uc_routing.config.loader`.
        """
        registry = cls()
        engine_cfg = config.get("routing_engine", {})
        accounts = {a["account_id"]: a for a in engine_cfg.get("accounts", [])}

        for route_id, route_cfg in config.get("routes", {}).items():
            account_id = route_cfg.get("account_id") or route_id
            account = accounts.get(account_id, {})
            route_type = route_cfg.get("type")
            if route_type is None:
                route_type = "anthropic"  # config convention: missing type == real Claude
            provider_type = ProviderType(route_type)
            registry.add(
                Route(
                    route_id=route_id,
                    provider_type=provider_type,
                    account_id=account_id,
                    model=route_cfg.get("model", route_id),
                    upstream=route_cfg.get("upstream"),
                    auth_ref=route_cfg.get("auth"),
                    headers=route_cfg.get("headers", {}),
                    body_overrides=route_cfg.get("body", {}),
                    capability=CapabilityProfile(
                        context_window=route_cfg.get("context_window", 128000),
                        supports_vision=route_cfg.get("supports_vision", False),
                        supports_tools=route_cfg.get("supports_tools", True),
                    ),
                    enabled=route_cfg.get("enabled", True),
                )
            )
        return registry
