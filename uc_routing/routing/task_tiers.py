"""Task tier classification."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class TaskTier(enum.Enum):
    PLANNING = "planning"
    HEAVY_REASONING = "heavy_reasoning"
    BULK_CONTEXT = "bulk_context"
    FRONTEND = "frontend"


@dataclass
class TaskTierDetector:
    """Detect task tier from request content and constraints.

    This placeholder uses heuristics. A production implementation may call a small
    classifier model and/or honor explicit routing directives.
    """

    default_tier: TaskTier = TaskTier.HEAVY_REASONING

    def detect(
        self,
        prompt: str,
        has_images: bool = False,
        tool_count: int = 0,
        estimated_tokens: int = 0,
        explicit_tier: Optional[str] = None,
    ) -> TaskTier:
        if explicit_tier:
            try:
                return TaskTier(explicit_tier)
            except ValueError:
                pass

        text = prompt.lower()

        if has_images:
            return TaskTier.FRONTEND

        bulk_keywords = ["summarize", "find all", "search", "bulk", "every", "all usages"]
        if any(k in text for k in bulk_keywords) or estimated_tokens > 16000:
            return TaskTier.BULK_CONTEXT

        planning_keywords = ["design", "plan", "architecture", "roadmap", "strategy"]
        if any(k in text for k in planning_keywords):
            return TaskTier.PLANNING

        heavy_keywords = ["debug", "reason", "prove", "concurrency", "algorithm"]
        if any(k in text for k in heavy_keywords) or tool_count > 5:
            return TaskTier.HEAVY_REASONING

        return self.default_tier
