"""Unit tests for telemetry schema and cost estimation."""

import unittest

from uc_routing.telemetry.cost import CostNormalizer
from uc_routing.telemetry.schema import RateLimitSnapshot, TelemetryEvent


class TestTelemetry(unittest.TestCase):
    def test_cost_normalizer(self):
        cn = CostNormalizer()
        estimate = cn.estimate(
            prompt_tokens=1000,
            completion_tokens=500,
            cost_per_input=1e-6,
            cost_per_output=2e-6,
            cost_per_request=0.005,
        )
        self.assertAlmostEqual(estimate.input_cost, 0.001)
        self.assertAlmostEqual(estimate.output_cost, 0.001)
        self.assertAlmostEqual(estimate.total, 0.007)

    def test_telemetry_event(self):
        event = TelemetryEvent(
            event_id="e1",
            request_id="r1",
            route_id="local-rtx5090",
            account_id="local-rtx5090",
            provider="ollama",
            model_id="llama-3.3-70b",
            tier="bulk_context",
            prompt_tokens=1000,
            completion_tokens=500,
        )
        self.assertEqual(event.outcome, "unknown")
        self.assertIsInstance(event.rate_limit, RateLimitSnapshot)


if __name__ == "__main__":
    unittest.main()
