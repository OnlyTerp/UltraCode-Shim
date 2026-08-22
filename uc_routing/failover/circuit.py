"""Circuit breaker state machine."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Simple circuit breaker per route/account."""

    failure_threshold: int = 3
    slow_request_threshold: int = 3
    open_duration_seconds: int = 30
    half_open_max: int = 1
    states: Dict[str, CircuitState] = field(default_factory=dict)
    failures: Dict[str, int] = field(default_factory=dict)
    opened_at: Dict[str, datetime] = field(default_factory=dict)
    half_open_count: Dict[str, int] = field(default_factory=dict)

    def state(self, route_id: str) -> CircuitState:
        st = self.states.get(route_id, CircuitState.CLOSED)
        if st == CircuitState.OPEN:
            opened = self.opened_at.get(route_id)
            if opened and datetime.now(timezone.utc) - opened > timedelta(
                seconds=self.open_duration_seconds
            ):
                self.states[route_id] = CircuitState.HALF_OPEN
                self.half_open_count[route_id] = 0
                return CircuitState.HALF_OPEN
        return st

    def record_success(self, route_id: str) -> None:
        self.states[route_id] = CircuitState.CLOSED
        self.failures[route_id] = 0

    def record_failure(self, route_id: str) -> None:
        self.failures[route_id] = self.failures.get(route_id, 0) + 1
        if self.failures[route_id] >= self.failure_threshold:
            self.states[route_id] = CircuitState.OPEN
            self.opened_at[route_id] = datetime.now(timezone.utc)

    def can_try(self, route_id: str) -> bool:
        st = self.state(route_id)
        if st == CircuitState.CLOSED:
            return True
        if st == CircuitState.OPEN:
            return False
        return self.half_open_count.get(route_id, 0) < self.half_open_max
