"""Abstract provider adapter.

Concrete adapters will wrap the existing providers/ helpers and translate
between Anthropic Messages API, OpenAI Chat Completions, and any special routes
like codex_oauth / cursor_agent.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Generator, Optional

from ..telemetry.schema import TelemetryEvent


class ProviderAdapter(ABC):
    """Issue a request to a provider and return a response stream/event."""

    @abstractmethod
    def send(
        self,
        route_id: str,
        request_body: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Yield streamed chunks and a final usage dict."""
        raise NotImplementedError

    @abstractmethod
    def health_check(self, route_id: str) -> Dict[str, Any]:
        """Return a lightweight health status dict."""
        raise NotImplementedError
