"""Unit tests for the ledger."""

import unittest

from uc_routing.ledger.models import Account, AccountKind, Entitlement, EntitlementKind


class TestLedger(unittest.TestCase):
    def test_account_has_capacity_for_window(self):
        ent = Entitlement(
            kind=EntitlementKind.FIXED_WINDOW,
            unit="messages",
            limit=5,
            used=0,
        )
        account = Account(
            account_id="test-anthropic",
            provider="anthropic",
            kind=AccountKind.SUBSCRIPTION,
            display_name="Test",
            entitlements=[ent],
        )
        self.assertTrue(account.has_capacity_for("messages", 1))

    def test_account_exhausted_window(self):
        ent = Entitlement(
            kind=EntitlementKind.FIXED_WINDOW,
            unit="messages",
            limit=5,
            used=5,
        )
        account = Account(
            account_id="test-anthropic",
            provider="anthropic",
            kind=AccountKind.SUBSCRIPTION,
            display_name="Test",
            entitlements=[ent],
        )
        self.assertFalse(account.has_capacity_for("messages", 1))


if __name__ == "__main__":
    unittest.main()
