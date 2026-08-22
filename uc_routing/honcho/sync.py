"""Honcho sync client placeholder.

Honcho endpoints and auth are configured under `routing_engine.honcho`.
This class is intentionally minimal; implementation will call the Honcho SDK or
REST API once the contract is finalized.
"""

from __future__ import annotations

from typing import Optional

from .contract import LedgerSnapshot, TelemetryBatch


class HonchoSyncClient:
    """Push/pull routing state to/from Honcho for cross-session consistency."""

    def __init__(
        self,
        app_id: str,
        user_id: str,
        base_url: str,
        api_key_ref: Optional[str] = None,
    ) -> None:
        self.app_id = app_id
        self.user_id = user_id
        self.base_url = base_url.rstrip("/")
        self.api_key_ref = api_key_ref

    def push_snapshot(self, snapshot: LedgerSnapshot, request_id: str) -> bool:
        """Push a ledger snapshot. Returns True on success."""
        # TODO: implement Honcho PUT/POST once API is known.
        return False

    def push_telemetry(self, batch: TelemetryBatch) -> bool:
        """Push a telemetry batch."""
        # TODO: implement Honcho telemetry ingest.
        return False

    def fetch_latest(self) -> Optional[LedgerSnapshot]:
        """Fetch the latest ledger snapshot for this user."""
        # TODO: implement Honcho GET.
        return None
