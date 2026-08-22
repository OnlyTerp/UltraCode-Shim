"""Unit tests for the routing engine."""

import unittest

from uc_routing.ledger.models import Account, AccountKind, Entitlement, EntitlementKind, Ledger
from uc_routing.providers.types import CapabilityProfile, ProviderType, Route
from uc_routing.providers.registry import RouteRegistry
from uc_routing.failover.health import HealthRegistry
from uc_routing.routing.engine import RoutingEngine
from uc_routing.telemetry.collector import TelemetryCollector


class TestRoutingEngine(unittest.TestCase):
    def test_select_route_prefers_local(self):
        local_route = Route(
            route_id="local-rtx5090",
            provider_type=ProviderType.LOCAL_OPENAI,
            account_id="local-rtx5090",
            model="llama-3.3-70b",
            capability=CapabilityProfile(
                context_window=128000,
                scores={"heavy_reasoning": 0.65, "bulk_context": 0.85},
            ),
        )
        cloud_route = Route(
            route_id="claude-opus",
            provider_type=ProviderType.ANTHROPIC,
            account_id="anthropic-pro",
            model="claude-opus-4-8",
            capability=CapabilityProfile(
                context_window=200000,
                scores={"heavy_reasoning": 0.98, "bulk_context": 0.85},
            ),
        )
        registry = RouteRegistry()
        registry.add(local_route)
        registry.add(cloud_route)

        ledger = Ledger()
        ledger.add_account(
            Account(
                account_id="local-rtx5090",
                provider="ollama",
                kind=AccountKind.LOCAL,
                display_name="Local RTX 5090",
                entitlements=[
                    Entitlement(
                        kind=EntitlementKind.LOCAL_COMPUTE,
                        unit="requests",
                        limit=2,
                        used=0,
                    )
                ],
            )
        )
        ledger.add_account(
            Account(
                account_id="anthropic-pro",
                provider="anthropic",
                kind=AccountKind.SUBSCRIPTION,
                display_name="Anthropic Pro",
                entitlements=[
                    Entitlement(
                        kind=EntitlementKind.FIXED_WINDOW,
                        unit="requests",
                        limit=100,
                        used=0,
                    )
                ],
            )
        )

        engine = RoutingEngine(
            ledger=ledger,
            registry=registry,
            health=HealthRegistry(),
            telemetry=TelemetryCollector(),
        )
        decision = engine.select_route("summarize all recent issues", prompt_tokens=500)
        self.assertEqual(decision.route_id, "local-rtx5090")


if __name__ == "__main__":
    unittest.main()
