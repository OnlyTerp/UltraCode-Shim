"""Load engine config from `config.json`."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .schema import HonchoConfig, LifeOSConfig, RoutingEngineConfig


@dataclass
class EngineConfig:
    """Top-level container returned by the loader."""

    config: RoutingEngineConfig
    source_path: Optional[str] = None


def load_engine_config(path: Optional[str] = None) -> EngineConfig:
    """Load `routing_engine` section from `config.json`.

    If `path` is omitted, reads `UC_CONFIG` env var, then `config.json`, then
    `config.example.json`, mirroring `proxy.py` behavior.
    """
    candidates = [
        path,
        os.environ.get("UC_CONFIG"),
        "config.json",
        "config.example.json",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = data.get("routing_engine", {})
            return EngineConfig(
                config=RoutingEngineConfig(
                    enabled=cfg.get("enabled", False),
                    tier_thresholds=cfg.get(
                        "tier_thresholds",
                        {
                            "planning": 0.80,
                            "heavy_reasoning": 0.90,
                            "bulk_context": 0.60,
                            "frontend": 0.70,
                        },
                    ),
                    honcho=HonchoConfig(**cfg.get("honcho", {})),
                    life_os=LifeOSConfig(**cfg.get("life_os", {})),
                    accounts=cfg.get("accounts", []),
                ),
                source_path=candidate,
            )
    return EngineConfig(config=RoutingEngineConfig())
