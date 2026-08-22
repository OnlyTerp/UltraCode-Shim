"""Data contracts for Honcho synchronization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class LedgerSnapshot:
    """Serializable ledger state for cross-device sync."""

    user_id: str
    sequence: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    accounts: List[Dict[str, Any]] = field(default_factory=list)
    cooldowns: List[Dict[str, Any]] = field(default_factory=list)
    circuit_states: Dict[str, str] = field(default_factory=dict)


@dataclass
class TelemetryBatch:
    """Batch of telemetry events ready to push to Honcho."""

    user_id: str
    request_id: str
    sequence: int
    events: List[Dict[str, Any]] = field(default_factory=list)
