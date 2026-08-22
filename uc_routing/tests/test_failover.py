"""Unit tests for failover primitives."""

import unittest

from uc_routing.failover.circuit import CircuitBreaker, CircuitState
from uc_routing.failover.cooldown import CooldownManager


class TestCircuitBreaker(unittest.TestCase):
    def test_opens_after_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        self.assertEqual(cb.state("r1"), CircuitState.CLOSED)
        cb.record_failure("r1")
        cb.record_failure("r1")
        self.assertEqual(cb.state("r1"), CircuitState.CLOSED)
        cb.record_failure("r1")
        self.assertEqual(cb.state("r1"), CircuitState.OPEN)
        self.assertFalse(cb.can_try("r1"))

    def test_success_closes(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure("r1")
        cb.record_success("r1")
        self.assertEqual(cb.state("r1"), CircuitState.CLOSED)


class TestCooldown(unittest.TestCase):
    def test_cooldown_active(self):
        cm = CooldownManager()
        cm.set("r1", "429", retry_after=60)
        self.assertTrue(cm.is_cooled("r1"))
        self.assertFalse(cm.is_cooled("r2"))


if __name__ == "__main__":
    unittest.main()
