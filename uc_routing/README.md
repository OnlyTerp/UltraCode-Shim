# `uc_routing` — Subscription- and Quota-Aware Routing Engine

This package is the implementation scaffold for the routing engine specified in
[`docs/ROUTING_ENGINE_SPEC.md`](../docs/ROUTING_ENGINE_SPEC.md). It is designed to
be wired into `proxy.py` later without disrupting the existing Auto Router until
the engine is stable.

## Design

- **Ledger** (`ledger/`) — tracks accounts, subscriptions, prepaid pools, and local compute capacity.
- **Telemetry** (`telemetry/`) — records per-request latency, token spend, rate limits, and quota state.
- **Routing** (`routing/`) — decides which provider/model to use for a given task tier.
- **Provider Adapters** (`providers/`) — wraps the existing `providers/` helpers and OpenAI/Anthropic endpoints.
- **Failover / Health** (`failover/`) — circuit breakers, cooldowns, health checks, and timeout policies.
- **Honcho Sync** (`honcho/`) — pushes ledger and telemetry state to Honcho for cross-device consistency.
- **Life OS Metrics** (`life_os/`) — exposes metrics for the terpOS / Life OS dashboard.
- **Config** (`config/`) — loads engine-specific settings from `config.json`.

## Status

Placeholder interfaces only. No production behavior is implemented yet. See the
spec for the full design, algorithms, and rollout plan.

## Running Tests

```bash
python3 -m unittest uc_routing.tests
```

## Integration

1. Add a `routing_engine` section to `config.json` (see spec Appendix A).
2. Import and instantiate `RoutingEngine` from `proxy.py`.
3. Route `POST /v1/messages` through `engine.select_route()` and dispatch via `uc_routing.providers` adapters.
4. Enable with `UC_ROUTING_ENGINE=1` once the implementation is complete.
