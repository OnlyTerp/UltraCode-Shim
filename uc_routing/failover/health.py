"""Health registry for routes and accounts."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional


class HealthState(enum.Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class HealthRecord:
    state: HealthState = HealthState.UNKNOWN
    last_check: Optional[datetime] = None
    last_success: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0


class HealthRegistry:
    """Track per-route health status."""

    def __init__(self) -> None:
        self.records: Dict[str, HealthRecord] = {}

    def record(self, route_id: str) -> HealthRecord:
        return self.records.setdefault(route_id, HealthRecord())

    def mark_healthy(self, route_id: str) -> None:
        r = self.record(route_id)
        r.state = HealthState.HEALTHY
        r.last_check = datetime.now(timezone.utc)
        r.last_success = r.last_check
        r.consecutive_failures = 0

    def mark_failure(self, route_id: str, error: Optional[str] = None) -> None:
        r = self.record(route_id)
        r.last_check = datetime.now(timezone.utc)
        r.last_error = error
        r.consecutive_failures += 1
        if r.consecutive_failures >= 3:
            r.state = HealthState.UNHEALTHY
        elif r.consecutive_failures >= 1:
            r.state = HealthState.DEGRADED

    def is_unhealthy(self, route_id: str) -> bool:
        r = self.records.get(route_id)
        if not r:
            return False
        return r.state == HealthState.UNHEALTHY
