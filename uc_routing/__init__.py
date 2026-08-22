"""Subscription- and quota-aware AI model routing engine for UltraCode-Shim.

This package is a scaffold. Importing it does not enable the engine; wire it into
`proxy.py` and flip `UC_ROUTING_ENGINE=1` when the implementation is ready.
"""

from .ledger.models import Account, AccountKind, Entitlement, Ledger
from .providers.types import CapabilityProfile, ProviderType, Route
from .routing.decision import RoutingDecision
from .routing.engine import RoutingEngine
from .routing.task_tiers import TaskTier, TaskTierDetector
from .telemetry.schema import TelemetryEvent

__all__ = [
    "Account",
    "AccountKind",
    "CapabilityProfile",
    "Entitlement",
    "Ledger",
    "ProviderType",
    "Route",
    "RoutingDecision",
    "RoutingEngine",
    "TaskTier",
    "TaskTierDetector",
    "TelemetryEvent",
]
