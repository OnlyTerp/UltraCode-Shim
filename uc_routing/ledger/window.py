"""Fixed-window and sliding-window entitlement accounting."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional


@dataclass
class WindowUsageEvent:
    """One reservation or spend against an entitlement window."""

    timestamp: datetime
    account_id: str
    entitlement_id: str
    route_id: str
    unit: str
    delta: float


class WindowTracker:
    """Track recent usage for sliding/calendar windows.

    This is a placeholder implementation using an in-memory deque. A production
    version should persist events and prune by the configured window.
    """

    def __init__(self) -> None:
        self.events: deque = deque()

    def add(self, event: WindowUsageEvent) -> None:
        self.events.append(event)

    def prune(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24 * 365)
        while self.events and self.events[0].timestamp < cutoff:
            self.events.popleft()

    def sum_for(
        self,
        account_id: str,
        entitlement_id: str,
        window_seconds: int,
        now: Optional[datetime] = None,
    ) -> float:
        """Return total `delta` within the last `window_seconds`."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=window_seconds)
        total = 0.0
        for ev in self.events:
            if ev.timestamp < cutoff:
                continue
            if ev.account_id == account_id and ev.entitlement_id == entitlement_id:
                total += ev.delta
        return total


@dataclass
class WindowedEntitlementLedger:
    """Container for window trackers keyed by account + entitlement."""

    trackers: Dict[str, WindowTracker] = field(default_factory=dict)

    def _key(self, account_id: str, entitlement_id: str) -> str:
        return f"{account_id}::{entitlement_id}"

    def reserve(
        self,
        account_id: str,
        entitlement_id: str,
        route_id: str,
        unit: str,
        delta: float,
    ) -> bool:
        """Record a usage reservation and return success."""
        key = self._key(account_id, entitlement_id)
        tracker = self.trackers.setdefault(key, WindowTracker())
        tracker.add(
            WindowUsageEvent(
                timestamp=datetime.now(timezone.utc),
                account_id=account_id,
                entitlement_id=entitlement_id,
                route_id=route_id,
                unit=unit,
                delta=delta,
            )
        )
        return True
