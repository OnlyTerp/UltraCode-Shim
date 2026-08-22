"""Core ledger domain models.

All monetary and token values use float only where unavoidable; most counts are
integers. This module intentionally contains no persistence logic.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class AccountKind(enum.Enum):
    SUBSCRIPTION = "subscription"
    PREPAID = "prepaid"
    LOCAL = "local"


class EntitlementKind(enum.Enum):
    FIXED_WINDOW = "fixed_window"
    PREPAID = "prepaid"
    LOCAL_COMPUTE = "local_compute"


@dataclass
class Entitlement:
    """A unit of capacity attached to an account."""

    kind: EntitlementKind
    unit: str  # e.g. "messages", "input_tokens", "output_tokens", "USD", "credits"
    limit: float  # for local compute, limit is max_concurrent slots
    used: float = 0.0
    remaining: float = 0.0
    window_type: Optional[str] = None  # sliding | calendar | session
    window_size_seconds: Optional[int] = None
    resets_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    cost_per_input_token: Optional[float] = None
    cost_per_output_token: Optional[float] = None
    cost_per_request: Optional[float] = None
    minimum_balance: Optional[float] = None
    max_concurrent: Optional[int] = None
    current_load: int = 0
    vram_total_mb: Optional[int] = None
    vram_reserved_mb: Optional[int] = None
    priority: Optional[str] = None  # always_first | tier_based | fallback_only
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SecretsRef:
    """Non-secret references to where credentials live (env vars, keyring)."""

    auth: Optional[str] = None
    api_key_ref: Optional[str] = None
    oauth_token_ref: Optional[str] = None


@dataclass
class Account:
    """A billable identity with one or more entitlements."""

    account_id: str
    provider: str
    kind: AccountKind
    display_name: str
    secrets_ref: SecretsRef = field(default_factory=SecretsRef)
    entitlements: List[Entitlement] = field(default_factory=list)
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def has_capacity_for(self, unit: str, amount: float = 1.0) -> bool:
        """Return True if any entitlement can satisfy `amount` of `unit`."""
        now = datetime.now(timezone.utc)
        for ent in self.entitlements:
            if ent.cooldown_until and ent.cooldown_until > now:
                continue
            if ent.unit != unit:
                continue
            if ent.kind == EntitlementKind.FIXED_WINDOW:
                if ent.resets_at and ent.resets_at <= now:
                    return True
                return (ent.used + amount) <= ent.limit
            if ent.kind in (EntitlementKind.PREPAID, EntitlementKind.LOCAL_COMPUTE):
                return (ent.used + amount) <= ent.limit
        return False


@dataclass
class Ledger:
    """In-memory collection of accounts and their live state."""

    accounts: Dict[str, Account] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def account(self, account_id: str) -> Optional[Account]:
        return self.accounts.get(account_id)

    def add_account(self, account: Account) -> None:
        self.accounts[account.account_id] = account

    def record(self, event: Dict[str, Any]) -> None:
        self.events.append(event)
