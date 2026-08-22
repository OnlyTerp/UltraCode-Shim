# Subscription- and Quota-Aware AI Model Routing Engine

**Status:** Design / scaffold  
**Scope:** UltraCode-Shim  
**Last updated:** 2026-08-22

This document specifies the next-generation routing layer for UltraCode-Shim. It supersedes the experimental `Auto Router` (`docs/AUTO_ROUTER.md`) with a ledger-aware engine that selects models based on real account entitlements, provider health, observed latency/quality telemetry, and task tier — while keeping the proxy itself dependency-light and stdlib-first.

---

## 1. Overview

### 1.1 Goals

- **Route every request to the cheapest capable backend** while respecting real-time quotas, rate limits, and user preferences.
- **Saturate zero-marginal-cost capacity first** (local RTX 5090 compute) before spending subscription or prepaid quota.
- **Prevent hangs on dead or empty routes** with deterministic timeouts, health checks, and circuit breakers.
- **Account for heterogeneous account types:** fixed-window subscriptions (Claude, ChatGPT, Cursor, Grok), prepaid credit pools (Devin, OpenRouter, API-key gateways), and local compute.
- **Emit fine-grained telemetry** (TTFT, end-to-end latency, token spend, remaining allowances, 429/cooldown events) to drive routing decisions and user dashboards.
- **Synchronize routing state to Honcho** so OnlyTerp's cross-session memory and cost state stays consistent.
- **Surface a Life OS metrics API** so `terpOS` (or any consumer) can display spend, quota, provider health, and task-tier mix.

### 1.2 Non-Goals

- This task is **spec and scaffold only**; no full provider integrations, no live inference, and no production enablement.
- We do **not** aim to replace the existing `proxy.py` in one PR; the engine is a parallel package (`uc_routing/`) that can be wired into `proxy.py` in a later migration.
- We do **not** build a new model-quality classifier from scratch in this scaffold; the existing `Auto Router` classifier path remains the default until the engine matures.
- We do **not** store provider secrets in the ledger; secrets continue to live in `config.json` / env vars / keychain and are resolved only inside provider adapters.

### 1.3 Terminology

| Term | Meaning |
|------|---------|
| **Account** | A billable identity with a provider (your Claude Pro account, your Devin org, your OpenRouter key, your local Ollama endpoint). |
| **Entitlement** | A unit of capacity attached to an account: a fixed-window subscription allowance, a prepaid credit balance, or a local compute slot. |
| **Provider** | A model-serving endpoint with a well-defined protocol (`anthropic`, `openai_compat`, `codex_oauth`, `cursor_agent`, `local_openai`). |
| **Route** | A concrete binding of a provider + account + model id + optional overrides. Mirrors `routes` in `config.json`. |
| **Task Tier** | A classification of a request by workload shape: `planning`, `heavy_reasoning`, `bulk_context`, `frontend`. |
| **Capability Card** | Structured metadata describing what a model/route can do, its context window, vision/tool support, and latency/cost class. |
| **Ledger** | The in-memory + persisted record of accounts, entitlements, quota usage, cooldowns, and circuit-breaker states. |

---

## 2. Domain Model

```
Request
  │
  ▼
[Classifier / Task-Tier Detector] ──▶ TaskTier + Constraints
  │
  ▼
[Routing Engine]
  │  reads: Ledger, Capability Index, Health Registry, Telemetry Cache
  │  writes: Telemetry, Ledger updates, Honcho sync, Life OS metrics
  │
  ▼
[Provider Adapter] ──▶ chosen Route ──▶ upstream model
  │
  ▼
[Telemetry Collector] ──▶ Ledger update + Honcho + Life OS
```

Core entities:

- `Request`: the inbound conversation/prompt, tools list, image attachments, desired max_tokens, and explicit routing hints (e.g. `[[route:opus]]`).
- `TaskTier`: one of `planning`, `heavy_reasoning`, `bulk_context`, `frontend`, plus a confidence score.
- `CapabilityProfile`: per-route metadata (context window, vision, tool support, reasoning, output quality per tier).
- `Route`: `route_id`, `provider_type`, `upstream`, `model`, `account_id`, `auth_ref`, `headers`, `body_overrides`, `capability_profile`.
- `Account`: `account_id`, `provider`, `account_kind` (`subscription`, `prepaid`, `local`), list of `Entitlement`s, metadata.
- `Entitlement`: `kind` (`fixed_window`, `prepaid`, `local_compute`), `window`, `limit`, `used`, `remaining`, `resets_at`, `cooldown_until`, `cost_per_token` or `cost_per_request`.
- `RoutingDecision`: `request_id`, chosen `route_id`, `tier`, `scores`, `fallback_chain`, `reason`, `estimated_cost`.
- `TelemetryEvent`: per-request metrics including TTFT, latency, tokens, outcome, rate-limit signals, and final ledger deltas.

---

## 3. Provider & Account Ledger

### 3.1 Provider Taxonomy

| Provider | Billing model | Quota semantics | Example route type |
|----------|--------------|-------------------|--------------------|
| **Anthropic (Claude)** | Subscription Pro/Max | Messages per N hours, input/output tokens per day, rate limits | `anthropic` passthrough |
| **OpenAI (ChatGPT/Codex)** | ChatGPT Plus / Codex credits | Per-day/hour message limits, rate limits, model-tier quotas | `codex_oauth` or `openai_compat` |
| **Cursor** | Subscription (Composer) | Rate-limited, no published hard cap; treat as soft quota + cooldown | `cursor_agent` |
| **Grok** | X Premium+ / API credits | Subscription window + prepaid API tier | `openai_compat` to xAI |
| **Devin** | Prepaid ACU/org credits | ACU balance consumed per session/operation | adapter bridge |
| **OpenRouter** | Prepaid credits + per-request cost | Balance decrements per token | `openai_compat` |
| **Generic API-key gateways** | Prepaid or metered | Balance or monthly limit | `openai_compat` |
| **Local compute** | Zero marginal | Concurrent slots, vRAM, tokens/sec throughput | `local_openai` / Ollama / vLLM |

### 3.2 Account Schema

```python
@dataclass
class Account:
    account_id: str          # stable, user-defined or derived
    provider: str            # e.g. "anthropic", "openrouter", "local_rtx5090"
    kind: AccountKind        # subscription | prepaid | local
    display_name: str
    secrets_ref: SecretsRef  # {"auth": "Bearer ${ANTHROPIC_API_KEY}", ...}
    entitlements: List[Entitlement]
    metadata: Dict[str, Any]
    enabled: bool = True
```

`account_id` must be unique per user/org. Multiple routes may share one account (e.g., several OpenRouter model slugs draw from the same OpenRouter balance).

### 3.3 Subscription Model (Fixed-Window)

Fixed-window subscriptions carry one or more `WindowedEntitlement`s:

- `window_type`: `rolling` or `calendar` (hourly, daily, monthly).
- `limit`: max allowed units in the window.
- `used`: units consumed in the current window.
- `resets_at`: timestamp when the window resets.
- `unit`: `requests`, `input_tokens`, `output_tokens`, `messages`.
- `overage_policy`: `block` (hard stop), `warn` (allow but mark), `spill_to` (route to prepaid fallback).

Examples:
- Claude Pro: 5x messages/4h for Opus; 50x messages/4h for Sonnet.
- ChatGPT Plus: ~40 messages/3h for GPT-4 class.
- Grok: rate-limited requests per minute/hour depending on tier.

### 3.4 Prepaid Credit Pools

Prepaid accounts use `BalanceEntitlement`:

- `currency`: USD, credits, ACU, OR-credits.
- `balance`: remaining units.
- `cost_per_input_token`, `cost_per_output_token`, `cost_per_request`.
- `minimum_balance`: stop routing when balance below this.
- `top_up_url`: optional link for manual top-up.

The ledger updates `balance` after every request using observed token counts (or, if unavailable, estimated from prompt/completion sizes).

### 3.5 Zero-Marginal-Cost Local Compute (RTX 5090)

Local accounts use `LocalComputeEntitlement`:

- `max_concurrent`: how many requests can run in parallel.
- `current_load`: in-flight count.
- `vram_total_mb`, `vram_reserved_mb`.
- `max_context`: model context length loaded.
- `tokens_per_second`: observed throughput.
- `priority`: routing priority (`always_first`, `tier_based`, `fallback_only`).

The router prefers local compute when:
- `priority` is `always_first` or the task tier is `bulk_context`/`frontend` and local can satisfy it.
- `current_load < max_concurrent` and available vRAM >= estimated model memory.
- The loaded model's capability profile scores >= the tier threshold.

---

## 4. Capability & Task-Tier Matrix

### 4.1 Task Tiers

| Tier | Typical request | Needs |
|------|----------------|-------|
| `planning` | "Design a multi-tenant auth system" | High reasoning, architecture, large context, reliable tool use. |
| `heavy_reasoning` | "Debug this non-deterministic concurrency bug" | Maximum reasoning, deep debugging, long chains, may need vision for screenshots. |
| `bulk_context` | "Summarize these 100 issues" / "Find all usages of X" | Huge context, cheap per token, moderate quality acceptable. |
| `frontend` | "Generate this React component from this screenshot" | Vision, fast, cheap, moderate reasoning; may prefer Claude/GPT-4o class. |

Tiers can be detected by:
- explicit directive (`[[tier:heavy]]`, `[[route:opus]]`, existing `[[route:...]]` syntax).
- heuristic: prompt length, presence of images, tool count, keywords ("plan", "refactor", "summarize", "find all").
- a small classifier model (same role as today's `classifier` in `Auto Router`).

### 4.2 Capability Profile

Each route has a `CapabilityProfile`:

```python
@dataclass
class CapabilityProfile:
    context_window: int
    supports_vision: bool
    supports_tools: bool
    supports_reasoning_split: bool
    scores: Dict[TaskTier, float]   # 0.0 .. 1.0
    cost_class: str                  # "free", "cheap", "mid", "premium"
    latency_class: str               # "fast", "normal", "slow"
    quality_class: str               # "local", "good", "great", "frontier"
    tags: List[str]                  # e.g. ["local", "32k-context", "vision"]
```

### 4.3 Example Capability/Task-Tier Matrix

| Route | Context | Vision | Tools | planning | heavy_reasoning | bulk_context | frontend |
|-------|---------|--------|-------|----------|-----------------|--------------|----------|
| `claude-opus` (Anthropic Pro) | 200K/1M | Yes | Yes | 0.95 | 0.98 | 0.85 | 0.90 |
| `claude-sonnet` | 200K | Yes | Yes | 0.90 | 0.90 | 0.90 | 0.90 |
| `gpt-5.5-codex` | 256K | Yes | Yes | 0.92 | 0.95 | 0.80 | 0.88 |
| `claude-mimo` | 1M | No | Yes | 0.75 | 0.65 | 0.95 | 0.60 |
| `claude-minimax-m3` | 1M | No | Yes | 0.80 | 0.75 | 0.95 | 0.55 |
| `local-llama-3.3-70b-rtx5090` | 128K | No | Yes | 0.60 | 0.55 | 0.85 | 0.50 |
| `local-qwen3-32b-rtx5090` | 128K | No | Yes | 0.70 | 0.65 | 0.90 | 0.60 |

The matrix is user-editable in `config.json`; the scaffold ships sensible defaults.

---

## 5. Routing Decision Algorithm

### 5.1 Inputs

- `request`: prompt, tools, images, context length, explicit hints.
- `tier`: detected or pinned task tier.
- `ledger`: accounts, entitlements, cooldowns, circuit states.
- `capability_index`: all routes with `CapabilityProfile`.
- `health_registry`: last health check per route, failure counts.
- `telemetry_cache`: recent latency and cost observations.

### 5.2 Selection Constraints

A route is **eligible** only if all of the following hold:

1. `enabled` in config and account `enabled`.
2. Capability score for `tier` >= `tier_threshold` (config per tier, default 0.70).
3. Context length of route >= estimated prompt length + requested `max_tokens`.
4. If images present, `supports_vision` is true.
5. If tools present, `supports_tools` is true.
6. Account has a usable entitlement:
   - fixed-window: `used < limit` and `now < resets_at` and `now > cooldown_until`.
   - prepaid: `balance > minimum_balance + estimated_cost`.
   - local: `current_load < max_concurrent` and enough vRAM.
7. Circuit breaker is `closed`.

### 5.3 Cost Function

For eligible routes, compute an `effective_cost` used for sorting:

```
monetary_cost = estimated_input_tokens * cost_per_input_token
              + estimated_output_tokens * cost_per_output_token
              + cost_per_request

capacity_pressure = used / limit            # for windowed entitlements
opportunity_cost  = monetary_cost * (1 + capacity_pressure)

latency_penalty   = observed_p95_latency_ms * latency_weight
quality_bonus     = -score * quality_weight  # higher score lowers effective cost

effective_cost    = opportunity_cost + latency_penalty + quality_bonus
```

Local compute uses `monetary_cost = 0` but may include a small `capacity_pressure` term to avoid overloading the GPU.

### 5.4 Priority Sorting

1. **Local zero-marginal-cost routes first**, sorted by capability score (desc), then load (asc).
2. **Subscription routes**, sorted by `effective_cost` (asc).
3. **Prepaid routes**, sorted by `effective_cost` (asc).
4. Any route with explicit `[[route:...]]` directive is promoted to the top of its tier group (it must still be eligible; if not, fail with a clear error).

### 5.5 Pseudocode

```python
def select_route(request, ledger, index, health, telemetry) -> RoutingDecision:
    request_id = generate_uuid()
    tier = detect_tier(request)  # may use classifier or hints

    candidates = []
    for route in index.routes:
        if not route.enabled:
            continue
        if not satisfies_constraints(route, request, tier):
            continue
        account = ledger.account(route.account_id)
        if not account.has_capacity_for(route, request):
            continue
        if health.circuit_is_open(route.route_id):
            continue

        score = route.capability.scores.get(tier, 0.0)
        if score < TIER_THRESHOLD[tier]:
            continue

        cost = estimate_effective_cost(route, account, request, telemetry)
        candidates.append((route, score, cost))

    # Priority: local first, then cost-sorted
    candidates.sort(key=lambda rc: (
        0 if route_is_local(rc[0]) else 1,
        -rc[1],      # higher capability first (tie-break)
        rc[2]         # lower effective cost
    ))

    if not candidates:
        return RoutingDecision(
            request_id=request_id,
            route_id=None,
            tier=tier,
            outcome="no_eligible_route",
            reason="No route satisfied constraints, quota, health, and circuit-breaker checks."
        )

    # Try primary and failover chain
    fallback_chain = [c[0] for c in candidates[:MAX_FAILOVER_DEPTH]]
    for route in fallback_chain:
        if try_reserve_capacity(route, ledger):
            return RoutingDecision(
                request_id=request_id,
                route_id=route.route_id,
                tier=tier,
                fallback_chain=fallback_chain,
                estimated_cost=cost_for(route, request),
                reason=f"capability={route.capability.scores[tier]:.2f}; effective_cost={cost:.4f}"
            )

    return RoutingDecision(request_id=request_id, route_id=None, tier=tier,
                           outcome="reservation_failed",
                           reason="All eligible routes failed capacity reservation.")
```

### 5.6 Failover on Runtime Errors

When a call fails:

- `429` / rate limit: mark route/account cooldown from `Retry-After` or exponential backoff; retry next candidate.
- `5xx` / timeout / connection error: increment failure counter; if threshold reached, open circuit breaker; retry next candidate.
- Empty or invalid response: retry same route up to `MAX_EMPTY_RETRIES`, then fail over.
- No remaining candidates: surface a structured error to the caller with the last error and suggestions (wait, top-up, switch tier).

A **dead or empty route** (no configured upstream, auth missing, model unavailable) is marked `unhealthy` immediately and skipped at selection time.

---

## 6. Quota-Window Accounting

### 6.1 Window Types

| Window type | Description |
|-------------|-------------|
| `sliding` | Most recent `window_size` of activity (e.g., last 4 hours). |
| `calendar` | Resets at fixed boundaries (midnight UTC, top of hour). |
| `session` | Counts within a single Claude Code session. |

### 6.2 Counter Implementation

- Maintain an append-only event log of `(timestamp, account_id, route_id, unit, delta)`.
- For sliding windows, keep events in a deque pruned by age.
- For calendar windows, snapshot `used` at reset boundaries.
- All counters are persisted to disk and synced to Honcho for cross-device consistency.

### 6.3 Over-Usage Protection

- **Pessimistic reservation**: decrement quota when a request starts; restore on failure.
- **Reconciliation**: after receiving actual usage from provider response headers, adjust the ledger to match.
- **Spill-over**: if a fixed-window entitlement is exhausted but the user has a prepaid account for the same provider/model, the request can spill to prepaid (configurable per account).

---

## 7. Health Checks, Timeouts, and Circuit Breakers

### 7.1 Health Checks

Each route has a lightweight health check performed periodically and on-demand:

- `GET /v1/models` or equivalent cheap endpoint.
- Measures: reachable, latency, auth accepted, model listed, `429`/`403` status.
- Health state: `healthy`, `degraded`, `unhealthy`, `unknown`.

### 7.2 Timeouts

| Tier | First-token timeout (TTFT) | Total timeout |
|------|---------------------------|---------------|
| `frontend` | 5s | 30s |
| `planning` | 10s | 120s |
| `heavy_reasoning` | 15s | 300s |
| `bulk_context` | 8s | 180s |

Per-route overrides are allowed in `config.json`.

### 7.3 Circuit Breakers

Per route/account:

- `failure_threshold`: number of consecutive failures before opening.
- `slow_request_threshold`: number of TTFT violations before marking degraded.
- `open_duration`: seconds the circuit stays open.
- `half_open_max`: number of probe requests allowed while half-open.

State machine: `closed -> open -> half_open -> closed` or `open`.

### 7.4 Cooldown Timers

- Triggered by explicit rate-limit (`429`) or `Retry-After` header.
- Stored in ledger as `cooldown_until` per account/entitlement.
- During cooldown, route is excluded unless user explicitly forces it.

---

## 8. Cost and Quota Telemetry

### 8.1 Telemetry Event Schema

```python
@dataclass
class TelemetryEvent:
    event_id: str
    request_id: str
    route_id: str
    account_id: str
    provider: str
    model_id: str
    tier: str
    timestamp: datetime
    ttft_ms: Optional[float]      # Time-To-First-Token
    e2e_latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    actual_cost: Optional[float]
    remaining_quota: Optional[float]
    rate_limit_headers: Dict[str, str]
    outcome: str                  # success, failure, cached, cancelled
    error_code: Optional[str]
    error_message: Optional[str]
    cached: bool
    fallback_index: int
    request_size_bytes: int
    response_size_bytes: int
```

### 8.2 TTFT and End-to-End Latency

- `ttft_ms` measured from request send to first streamed chunk.
- `e2e_latency_ms` measured from request start to final chunk/complete.
- Latency histograms are kept per route and tier (p50, p95, p99).

### 8.3 Token Spend and Remaining Allowances

- Token counts sourced from provider response body `usage` or response headers.
- For `openai_compat`, read `usage.prompt_tokens` / `completion_tokens`.
- For Anthropic passthrough, read `usage.input_tokens` / `output_tokens`.
- If missing, estimate using the same tokenizer approximation used for routing.
- Deduct from the appropriate entitlement and record `remaining_quota`.

### 8.4 Rate-Limit / 429 Detection

- Parse `x-ratelimit-*`, `Retry-After`, `x-ratelimit-remaining`, `x-ratelimit-reset`.
- Normalize to a `RateLimitSnapshot`:
  - `requests_remaining`, `requests_reset`
  - `tokens_remaining`, `tokens_reset`
- Trigger cooldown when `remaining == 0` or `429` returned.

### 8.5 Cooldown and Backoff Telemetry

- Record cooldown events: `route_id`, `reason` (`429`, `timeout`, `manual`), `cooldown_until`.
- Exponential backoff per account (1s, 2s, 4s, ... capped at `max_cooldown`).
- Reset on successful health check.

---

## 9. Failover Policy

### 9.1 Failover Triggers

- Provider returns HTTP error (`>= 500`, `429`, `403` with auth failure).
- Request exceeds TTFT or total timeout.
- Empty or malformed response after `MAX_EMPTY_RETRIES`.
- Circuit breaker opens during the request.
- Quota exhausted mid-flight (returned by provider).

### 9.2 Failover Order

The `fallback_chain` produced by the routing algorithm is the ordered list of candidates. The engine tries each in order, applying backoff/cooldown updates after each failure, until:

- a candidate succeeds,
- the chain is exhausted,
- or the user cancels.

### 9.3 Preventing Hangs on Dead/Empty Routes

- Routes with no `upstream`, missing `model`, or unresolved `${VAR}` are marked `unhealthy` at config load and excluded.
- Selection always computes the full fallback chain before the first outbound request.
- Each attempt has a hard deadline; if the overall request deadline is exceeded, abort and return a structured error.
- No request is allowed to block on a single provider for longer than the per-tier total timeout.

---

## 10. Honcho Synchronization Contract

### 10.1 What We Sync

Honcho stores durable, cross-session state for the OnlyTerp user. The engine pushes:

- `LedgerSnapshot`: accounts, entitlements, cooldowns, circuit breaker states.
- `TelemetryBatch`: recent `TelemetryEvent`s.
- `RoutingDecisionLog`: recent decisions for explainability.

### 10.2 Sync API (placeholder)

```python
class HonchoSyncClient:
    def __init__(self, app_id: str, user_id: str, base_url: str, api_key_ref: str): ...
    def push_snapshot(self, snapshot: LedgerSnapshot, request_id: str) -> None: ...
    def push_telemetry(self, batch: List[TelemetryEvent]) -> None: ...
    def fetch_latest(self) -> Optional[LedgerSnapshot]: ...
```

### 10.3 Consistency Model

- Sync is **asynchronous and best-effort**; routing continues if Honcho is unreachable.
- Each sync payload carries a `request_id` and monotonic `sequence` number for idempotency.
- Honcho merges snapshots by `sequence` (last-write-wins per account/entitlement).
- Initial load attempts to hydrate the ledger from Honcho at startup; if unavailable, fall back to local disk.

---

## 11. Life OS Metrics/API

Life OS (terpOS) displays real-time cost/quota/health dashboards. The engine exposes an internal metrics surface that can be consumed by `terpOS` or any authorized client.

### 11.1 Metrics Endpoints

```
GET /life-os/metrics/routing
GET /life-os/metrics/providers
GET /life-os/metrics/cost
GET /life-os/metrics/health
GET /life-os/metrics/tiers
WS  /life-os/metrics/stream
```

### 11.2 Metrics Payload

```python
@dataclass
class RoutingMetrics:
    window: str                  # e.g., "1h", "24h", "7d"
    total_requests: int
    requests_by_tier: Dict[str, int]
    requests_by_provider: Dict[str, int]
    total_estimated_cost: float
    cost_by_provider: Dict[str, float]
    avg_ttft_ms: float
    p95_ttft_ms: float
    avg_e2e_latency_ms: float
    p95_e2e_latency_ms: float
    quota_remaining: Dict[str, float]
    provider_health: Dict[str, str]
    active_cooldowns: List[CooldownRecord]
```

### 11.3 Push vs Pull

- **Pull:** `terpOS` polls the metrics endpoints.
- **Push:** optional `UC_LIFE_OS_WEBSOCKET` pushes streaming events when enabled.

---

## 12. Security and Secret Handling

- Secrets (API keys, OAuth tokens) **never** enter the ledger or telemetry; they live only in `config.json` (gitignored) or environment variables.
- Provider adapters resolve `${VAR}` references at request time and never log resolved values.
- Ledger persistence is encrypted at rest when possible (e.g., via OS keyring or `cryptography` if user opts in); otherwise stored in the user state directory.
- Honzo/Life OS traffic is sent over TLS with authenticated `Authorization` headers.
- The proxy's existing `GUARD_LOCAL` Host-header checks remain active; no new admin endpoints are exposed beyond `/healthz`, `/metrics`, and `/life-os/*`.
- Audit: every `RoutingDecision` and quota mutation is logged with `request_id` for traceability.

---

## 13. Consistency and Idempotency

### 13.1 Request IDs

- A single `request_id` is generated at the routing boundary and propagated through provider calls, telemetry, ledger updates, and Honcho sync.
- All downstream events are keyed by `request_id` and `event_id`.

### 13.2 Ledger Atomicity

- Quota reservation and spend updates are applied in memory first, then persisted to disk and Honcho asynchronously.
- A crash between in-memory update and persistence is recovered on next startup by replaying the local event log or fetching from Honcho.

### 13.3 Idempotent Sync

- Sync payloads include `sequence` and `request_id`.
- Honcho rejects older sequences and deduplicates by `request_id`.

---

## 14. Observability

### 14.1 Logging

- Structured logs with `request_id`, `route_id`, `tier`, `outcome`.
- `UC_ROUTING_LOG=1` enables decision logging (same spirit as `UC_ROUTER_LOG`).
- Log levels: `INFO` for routing decisions, `WARN` for quota/cooldown events, `ERROR` for failover exhaustion.

### 14.2 Metrics

- Counters: `routing_requests_total`, `routing_failovers_total`, `routing_quota_exhausted_total`.
- Histograms: `routing_latency_ms`, `provider_ttft_ms`, `provider_e2e_latency_ms`.
- Gauges: `provider_health`, `quota_remaining`, `active_cooldowns`, `circuit_breaker_state`.

### 14.3 Tracing

- Minimal in-process spans: `classify_tier`, `select_route`, `provider_call`, `update_ledger`, `sync_honcho`.
- Optional OpenTelemetry exporter; core engine remains stdlib-only.

---

## 15. Testing Strategy

### 15.1 Unit Tests

- `test_ledger.py`: windowed/prepaid/local entitlement accounting, cooldowns, spill-over.
- `test_routing.py`: constraint satisfaction, cost sorting, tier detection, failover chain ordering.
- `test_failover.py`: circuit breaker transitions, cooldown backoff, timeout handling.
- `test_telemetry.py`: event schema, rate-limit header parsing, token reconciliation.
- `test_honcho_sync.py`: snapshot serialization, idempotent push, fetch fallback.

### 15.2 Integration Tests

- Mock provider servers (same pattern as `test_proxy.py`) for Anthropic, OpenAI-compatible, and local backends.
- End-to-end scenarios:
  - route a request to local compute first.
  - exhaust a fixed-window quota and spill to prepaid.
  - trigger 429 and verify cooldown + failover.
  - open circuit breaker after repeated failures.

### 15.3 Property Tests

- Quota windows: for all window types and sizes, `used` is always <= `limit` after reservation, and resets happen exactly once per window.
- Failover: with at least one healthy route, every request eventually succeeds or returns a structured `no_eligible_route`.
- Cost: the selected route is never more expensive than another eligible route with higher or equal capability and lower actual cost, unless explicitly pinned.

### 15.4 Load / Concurrency Tests

- Many concurrent requests hit the router; verify:
  - no over-commit of local compute slots.
  - ledger counters remain accurate.
  - circuit breakers do not flap.

---

## 16. Rollout and Migration Plan

### Phase 1: Spec and scaffold (this PR)

- Merge `docs/ROUTING_ENGINE_SPEC.md` and the `uc_routing/` package with placeholder interfaces.
- No changes to `proxy.py` behavior.

### Phase 2: Ledger and telemetry in memory

- Implement in-memory `Ledger`, `TelemetryCollector`, and `RoutingEngine`.
- Add unit tests.

### Phase 3: Provider adapter integration

- Refactor existing `proxy.py` route dispatch into `uc_routing/providers/adapter.py` wrappers.
- Keep `proxy.py` as the HTTP server and protocol translator; routing decisions move into `uc_routing/`.
- Add integration tests with mock backends.

### Phase 4: Honcho and Life OS integration

- Implement `HonchoSyncClient` and `LifeOSMetrics` behind config flags.
- Add persistence and cross-device sync.

### Phase 5: Gradual enablement

- Introduce `UC_ROUTING_ENGINE=0/1` (default `0`).
- Run A/B against `Auto Router`.
- When stable, make it the default and deprecate the old `router` block.

---

## 17. Open Questions and Assumptions

### Assumptions

1. The engine is implemented in Python 3.8+, using only the standard library for core logic; optional integrations (Honcho SDK, OpenTelemetry) may be installed by the user but are not required.
2. Existing `config.example.json` remains the canonical user-facing config; engine-specific config lives under a new `routing_engine` key.
3. Provider secrets continue to be interpolated from `${VAR}` at request time and are never persisted by the ledger.
4. Local compute is exposed as an OpenAI-compatible HTTP endpoint (Ollama, LM Studio, vLLM, llama.cpp server) on `127.0.0.1`.
5. Honcho and Life OS base URLs and auth are configured under `routing_engine.honcho` and `routing_engine.life_os`.

### Open Questions

1. What is the exact Honcho app/user ID scheme and API surface? (The `HonchoSyncClient` is a placeholder until this is finalized.)
2. Should Cursor Composer be modeled as a first-class route with its own quota semantics, or kept as an experimental `cursor_agent` provider?
3. How do we obtain accurate rate-limit headers from Devin and Grok? Do they expose the same `x-ratelimit-*` conventions?
4. Should the classifier for tier detection be a separate cheap model call, or can we reuse the existing `Auto Router` classifier and add a tier output?
5. What is the desired granularity of Life OS dashboards (per minute, per hour, per session)?
6. Should the engine support multi-user/org ledger separation, or is it single-user per proxy instance?
7. Do we need a SQLite backend for the ledger, or is JSON-on-disk + Honcho sufficient for v1?
8. How should we normalize cost across different credit types (USD, OpenRouter credits, ACU)?

---

## 18. Repo Structure Scaffold

The following package is added under `uc_routing/` and is designed to be wired into `proxy.py` later without disrupting the current `providers/`, `scripts/`, `examples/`, or `docs/` layout.

```
UltraCode-Shim/
├── docs/
│   └── ROUTING_ENGINE_SPEC.md          # this document
├── uc_routing/
│   ├── __init__.py                     # public API exports
│   ├── README.md                       # package overview and integration notes
│   ├── ledger/
│   │   ├── __init__.py
│   │   ├── models.py                   # Account, Entitlement, Ledger
│   │   ├── window.py                   # fixed-window accounting
│   │   └── balance.py                  # prepaid/local compute accounting
│   ├── telemetry/
│   │   ├── __init__.py
│   │   ├── schema.py                   # TelemetryEvent, RateLimitSnapshot
│   │   ├── collector.py                # in-memory collector + histograms
│   │   └── cost.py                     # cost estimation / normalization
│   ├── routing/
│   │   ├── __init__.py
│   │   ├── engine.py                   # RoutingEngine.select_route()
│   │   ├── policy.py                   # selection, sorting, failover policy
│   │   ├── decision.py                 # RoutingDecision dataclass
│   │   └── task_tiers.py               # TaskTier enum + detection
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── types.py                    # ProviderType, CapabilityProfile
│   │   ├── adapter.py                  # Abstract / concrete adapter interface
│   │   └── registry.py                 # route index from config.json
│   ├── failover/
│   │   ├── __init__.py
│   │   ├── health.py                   # health checks and registry
│   │   ├── circuit.py                  # circuit breaker
│   │   └── cooldown.py                 # cooldown/backoff timers
│   ├── honcho/
│   │   ├── __init__.py
│   │   ├── contract.py                 # data contracts for sync
│   │   └── sync.py                     # HonchoSyncClient placeholder
│   ├── life_os/
│   │   ├── __init__.py
│   │   ├── api.py                      # metrics endpoints / handlers
│   │   └── metrics.py                  # RoutingMetrics + aggregation
│   ├── config/
│   │   ├── __init__.py
│   │   ├── schema.py                   # engine-specific config dataclasses
│   │   └── loader.py                   # load engine config from config.json
│   └── tests/
│       ├── __init__.py
│       ├── test_ledger.py
│       ├── test_routing.py
│       ├── test_failover.py
│       └── test_telemetry.py
├── proxy.py                            # existing; integration point for later
├── test_proxy.py                       # existing
├── config.example.json                 # existing; engine section added later
└── README.md                           # existing
```

### Integration Notes

- `uc_routing` is a sibling to the existing `providers/` directory. The new `uc_routing/providers/adapter.py` will eventually wrap the existing `providers/codex_oauth.py` and `providers/cursor_agent.py` helpers, plus add new adapters for Anthropic passthrough and OpenAI-compatible endpoints.
- `uc_routing/config/loader.py` reads the same `config.json` used by `proxy.py` and expects engine config under a top-level `routing_engine` key. If the key is absent, the engine disables itself and `proxy.py` continues using the existing `Auto Router`.
- The engine is designed to be **feature-flagged** (`UC_ROUTING_ENGINE`) so the existing proxy behavior is unchanged until explicitly enabled.

---

## Appendix A: Sample Config Snippet

```jsonc
{
  "_routing_engine": "New subscription/quota-aware routing. Set enabled:true to opt in. See docs/ROUTING_ENGINE_SPEC.md.",
  "routing_engine": {
    "enabled": false,
    "tier_thresholds": {
      "planning": 0.80,
      "heavy_reasoning": 0.90,
      "bulk_context": 0.60,
      "frontend": 0.70
    },
    "honcho": {
      "enabled": false,
      "base_url": "${HONCHO_BASE_URL}",
      "app_id": "onlyterp-routing",
      "api_key_ref": "${HONCHO_API_KEY}"
    },
    "life_os": {
      "enabled": false,
      "base_url": "${LIFE_OS_BASE_URL}",
      "push_stream": false
    },
    "accounts": [
      {
        "account_id": "anthropic-pro",
        "provider": "anthropic",
        "kind": "subscription",
        "entitlements": [
          {"kind": "fixed_window", "unit": "messages", "window_type": "sliding", "window_size_hours": 4, "limit": 5}
        ]
      },
      {
        "account_id": "openrouter-pool",
        "provider": "openrouter",
        "kind": "prepaid",
        "entitlements": [
          {"kind": "prepaid", "currency": "USD", "balance": 25.00, "cost_per_input_token": 0.0000005, "cost_per_output_token": 0.0000015}
        ]
      },
      {
        "account_id": "local-rtx5090",
        "provider": "ollama",
        "kind": "local",
        "entitlements": [
          {"kind": "local_compute", "max_concurrent": 2, "vram_total_mb": 24576, "priority": "always_first"}
        ]
      }
    ]
  }
}
```

---

*End of specification. Implementation placeholders live in `uc_routing/`.*
