"""Failover primitives: health checks, circuit breakers, cooldowns."""

from .circuit import CircuitBreaker
from .cooldown import CooldownManager
from .health import HealthRegistry, HealthState

__all__ = ["CircuitBreaker", "CooldownManager", "HealthRegistry", "HealthState"]
