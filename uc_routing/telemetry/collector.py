"""In-memory telemetry collector and simple histograms."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .schema import TelemetryEvent


@dataclass
class LatencyHistogram:
    """Simple latency statistics (placeholder for p50/p95)."""

    values: List[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.values.append(value)

    def percentile(self, p: float) -> Optional[float]:
        if not self.values:
            return None
        sorted_vals = sorted(self.values)
        idx = int(round(p / 100.0 * (len(sorted_vals) - 1)))
        return sorted_vals[idx]


class TelemetryCollector:
    """Collect events and compute per-route latency summaries."""

    def __init__(self) -> None:
        self.events: List[TelemetryEvent] = []
        self.latency_by_route: Dict[str, LatencyHistogram] = {}

    def record(self, event: TelemetryEvent) -> None:
        self.events.append(event)
        hist = self.latency_by_route.setdefault(event.route_id, LatencyHistogram())
        if event.e2e_latency_ms is not None:
            hist.add(event.e2e_latency_ms)

    def recent_events(self, count: int = 100) -> List[TelemetryEvent]:
        return self.events[-count:]

    def p95_latency_ms(self, route_id: str) -> Optional[float]:
        hist = self.latency_by_route.get(route_id)
        return hist.percentile(95) if hist else None

    def flush(self) -> List[TelemetryEvent]:
        """Return and clear in-memory events (push to persistence/Honcho)."""
        events = self.events
        self.events = []
        return events
