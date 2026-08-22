"""Provider and route domain types."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class ProviderType(enum.Enum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPAT = "openai_compat"
    CODEX_OAUTH = "codex_oauth"
    CURSOR_AGENT = "cursor_agent"
    LOCAL_OPENAI = "local_openai"


@dataclass
class CapabilityProfile:
    """Structured capability metadata for a route/model."""

    context_window: int = 8192
    supports_vision: bool = False
    supports_tools: bool = True
    supports_reasoning_split: bool = False
    scores: Dict[str, float] = field(default_factory=dict)
    cost_class: str = "mid"  # free | cheap | mid | premium
    latency_class: str = "normal"  # fast | normal | slow
    quality_class: str = "good"  # local | good | great | frontier
    tags: list = field(default_factory=list)


@dataclass
class Route:
    """A concrete provider + account + model binding."""

    route_id: str
    provider_type: ProviderType
    account_id: str
    model: str
    upstream: Optional[str] = None
    auth_ref: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    body_overrides: Dict[str, Any] = field(default_factory=dict)
    capability: Optional[CapabilityProfile] = None
    enabled: bool = True
    max_output_tokens: Optional[int] = None
