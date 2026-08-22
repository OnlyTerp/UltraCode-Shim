"""Telemetry event and rate-limit schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class RateLimitSnapshot:
    """Normalized rate-limit state from a provider response."""

    requests_remaining: Optional[int] = None
    requests_reset: Optional[datetime] = None
    tokens_remaining: Optional[float] = None
    tokens_reset: Optional[datetime] = None
    retry_after_seconds: Optional[int] = None
    raw_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class TelemetryEvent:
    """One routing/provider request plus its outcome."""

    event_id: str
    request_id: str
    route_id: str
    account_id: str
    provider: str
    model_id: str
    tier: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ttft_ms: Optional[float] = None
    e2e_latency_ms: Optional[float] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    actual_cost: Optional[float] = None
    remaining_quota: Optional[float] = None
    rate_limit: RateLimitSnapshot = field(default_factory=RateLimitSnapshot)
    outcome: str = "unknown"  # success | failure | cached | cancelled
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    cached: bool = False
    fallback_index: int = 0
    request_size_bytes: int = 0
    response_size_bytes: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def token_cost_estimate(
        self,
        cost_per_input: float = 0.0,
        cost_per_output: float = 0.0,
        cost_per_request: float = 0.0,
    ) -> float:
        return (
            self.prompt_tokens * cost_per_input
            + self.completion_tokens * cost_per_output
            + cost_per_request
        )
