"""Cooldown / backoff timer management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional


@dataclass
class CooldownRecord:
    route_id: str
    reason: str
    cooldown_until: datetime
    retry_after_seconds: Optional[int] = None


class CooldownManager:
    """Track active cooldowns per route/account and compute exponential backoff."""

    def __init__(self, max_cooldown_seconds: int = 3600) -> None:
        self.cooldowns: Dict[str, CooldownRecord] = {}
        self.consecutive_failures: Dict[str, int] = {}
        self.max_cooldown_seconds = max_cooldown_seconds

    def set(
        self,
        route_id: str,
        reason: str = "429",
        retry_after: Optional[int] = None,
    ) -> CooldownRecord:
        failures = self.consecutive_failures.get(route_id, 0) + 1
        self.consecutive_failures[route_id] = failures

        if retry_after:
            seconds = retry_after
        else:
            seconds = min(2 ** failures, self.max_cooldown_seconds)

        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        record = CooldownRecord(
            route_id=route_id,
            reason=reason,
            cooldown_until=until,
            retry_after_seconds=seconds,
        )
        self.cooldowns[route_id] = record
        return record

    def clear(self, route_id: str) -> None:
        self.cooldowns.pop(route_id, None)
        self.consecutive_failures.pop(route_id, None)

    def is_cooled(self, route_id: str) -> bool:
        record = self.cooldowns.get(route_id)
        if not record:
            return False
        return datetime.now(timezone.utc) < record.cooldown_until
