"""Typed configuration schema for the routing engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class HonchoConfig:
    enabled: bool = False
    base_url: Optional[str] = None
    app_id: str = "onlyterp-routing"
    api_key_ref: Optional[str] = None


@dataclass
class LifeOSConfig:
    enabled: bool = False
    base_url: Optional[str] = None
    push_stream: bool = False


@dataclass
class RoutingEngineConfig:
    enabled: bool = False
    tier_thresholds: Dict[str, float] = field(
        default_factory=lambda: {
            "planning": 0.80,
            "heavy_reasoning": 0.90,
            "bulk_context": 0.60,
            "frontend": 0.70,
        }
    )
    honcho: HonchoConfig = field(default_factory=HonchoConfig)
    life_os: LifeOSConfig = field(default_factory=LifeOSConfig)
    accounts: List[Dict[str, Any]] = field(default_factory=list)
