<p align="center">
  <img src="../assets/brand/doc-how.png" alt="How it works — the loopback proxy, the UltraCode envelope, and the evidence" width="100%">
</p>

UltraCode-Shim is a tiny loopback proxy plus a launcher. There is no magic and
no secret model. This page explains the mechanism and the reverse-engineering it
is based on.

## 1. What "UltraCode" actually is

At the Anthropic API boundary, Claude Code's **UltraCode** mode is not a hidden
model — it's an *envelope* applied to an ordinary `/v1/messages` request:

| Field | UltraCode value | Meaning |
|-------|-----------------|---------|
| `output_config.effort` | `"xhigh"` | maximum reasoning effort |
| `thinking` | `{"type": "adaptive"}` | extended/adaptive thinking on |
| `max_tokens` | `>= 64000` | room for long, thorough answers |
| `system` | + an *"Ultracode is on…"* reminder block | steers toward the Workflow/quality harness |

That's it. Anything that speaks the Anthropic Messages API and honors those
fields gets the UltraCode treatment. Because it's just request shape, we can put
the *same* envelope on a request and then forward it to **any** backend.

## 2. The proxy

`proxy.py` is a standard-library HTTP server you point Claude Code at via
`ANTHROPIC_BASE_URL` (the launchers do this for you). For every request it:

1. **Forces the envelope** on `POST /v1/messages` — sets `effort=xhigh`, adaptive
   `thinking`, raises `max_tokens` to the floor (default 64000), and injects the
   reminder if it isn't already present. (Toggle with `UC_FORCE_EFFORT`,
   `UC_FORCE_THINKING`, `UC_MAX_TOKENS`, `UC_INJECT_REMINDER`.)
2. **Serves `GET /v1/models`**, merging Anthropic's real model list with your
   own entries from `config.json` so they show up in the `/model` picker.
3. **Routes** each model id Claude Code sends to a real backend, per the
   `routes` map in `config.json`.

## 3. Why your models appear in `/model` (gateway discovery)

Recent Claude Code supports **gateway model discovery**: when
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`, it calls `GET /v1/models` on the
gateway and lists what comes back in `/model`. The launchers set that env var and
also pre-seed Claude Code's `cache/gateway-models.json` so your models show on
the very first open.

The proxy's `GET /v1/models` returns the union of three sources, deduped by id:

1. **Stock Claude models** — real Claude (Opus/Sonnet/Haiku), *always* served so
   it never drops out of the picker. This list is **self-updating**: see below.
2. **Anthropic's own `/v1/models`** — fetched live and merged in when a credential
   is available to forward; if that fetch can't run (no key, offline, a hiccup)
   the stock list above stands in for it.
3. **Your configured models** — everything in `config.json` → `models`, plus the
   synthesized `Worker → X` entries.

So even with **no Anthropic key to list models upstream**, you still see real
Claude *and* your own backends. (Earlier builds served custom-only on an upstream
miss, which is why Opus could vanish from `/model`.) Turn the stock list off with
`proxy.include_stock_models: false` or `UC_INCLUDE_STOCK_MODELS=0`; override the
exact list with `UC_STOCK_MODELS`. The stock ids are advertised only — they are
**not** orchestrator/worker picks, so the workflow's hardcoded `claude-opus-4-8`
background traffic is still remapped onto your pick (see §5) instead of hijacking
the selection.

**Self-updating stock list (so a future Opus just appears).** The built-in stock
list is only a *baseline*. Whenever the proxy makes a **successful** upstream
`/v1/models` fetch, it learns the real Claude ids Anthropic returns (with their
display names), keeps the Claude-only ones, and caches them to disk
(`%LOCALAPPDATA%\UltraCode-Shim\stock-models.json` on Windows, or
`$XDG_STATE_HOME/ultracode-shim/stock-models.json`). On later runs — including
when upstream is unreachable — that learned list is used (with the baseline
filling in anything not learned yet). Net effect: when Anthropic ships the next
Opus, it shows up in `/model` automatically, **no update to this tool required**.
Learned ids win over the baseline; an explicit `UC_STOCK_MODELS` still wins over
both. Disable learning with `proxy.learn_stock_models: false` /
`UC_STOCK_LEARN=0`, point the cache elsewhere with `UC_STOCK_CACHE`, and watch it
on `/healthz` (`stock_learning`).

> **Hard rule from Claude Code:** discovered ids are filtered with
> `/^(claude|anthropic)/i`. Any model id that does **not** start with `claude`
> or `anthropic` is silently dropped. That's why every `id` in `config.json`
> looks like `claude-mimo`, `claude-openrouter`, etc. (The proxy applies the same
> rule to the stock list and any `UC_STOCK_MODELS` override.)

Gateway discovery only triggers on a first-party (OAuth) login, not on a raw
`ANTHROPIC_API_KEY`.

## 4. Routing each pick to a real backend

When you pick a model, Claude Code sends its id as `model`. The proxy looks that
id up in `config.json` → `routes` and forwards accordingly:

- **Anthropic passthrough** (no `type`, or `type: "anthropic"`) — forwards the
  request unchanged to `upstream` (default `api.anthropic.com`, i.e. real
  Claude) or any Anthropic-compatible endpoint. Tools work natively.
- **`openai_compat`** — translates the Anthropic request to an OpenAI
  Chat Completions request, POSTs it to `upstream + /chat/completions`, and
  translates the response back. **Tool calls are translated both ways**
  (Anthropic `tool_use`/`tool_result` ⇄ OpenAI `tool_calls`/`role:tool`), and
  streaming SSE is re-emitted as Anthropic SSE. This covers MiMo, DeepSeek,
  StepFun, Ollama, OpenRouter, OpenAI, local llama.cpp/LM Studio, etc.
- **`codex_oauth`** — sends to GPT‑5.5 via your ChatGPT/Codex *login* (no API
  key), using `providers/codex_oauth.py` and the token from `codex login`.
- **`grok_build`** — sends to xAI Grok via your Grok *login* (SuperGrok / X
  subscription, no API key). `providers/grok_build.py` reads the token from
  `grok login --oauth` (`~/.grok/auth.json`), refreshes it against `auth.x.ai`,
  and reuses the `openai_compat` path pointed at `api.x.ai` — Grok speaks plain
  Chat Completions, so tools and streaming work unchanged.
- **`cursor_agent`** (experimental) — bridges to Cursor's Composer through the
  `cursor-agent` CLI via `providers/cursor_agent.py`. Reasoning works well;
  tool-calling is a best-effort text bridge.

## 5. Orchestrator + Worker (two-model dynamic workflows)

Claude Code's `/model` picker is single-slot, but its **dynamic-workflow** engine
spawns many background/sub-agent calls — and it issues most of them as the stock
model id (`claude-opus-4-8`) regardless of your pick. So the sub-agents that do
the bulk of a workflow's work don't follow your selection, and can bill a model
you didn't choose.

The proxy fixes this by holding a **sticky two-tier selection** and routing every
request by tier:

| Tier | What it is | How it's detected | Routes to |
|------|-----------|-------------------|-----------|
| `heavy` | the orchestrator (main interactive loop) | the request carries an **interactive-only tool** (`AskUserQuestion` / `EnterPlanMode` / `ExitPlanMode`) that the harness only ever gives the main loop | your **orchestrator** model |
| `fast` | every Workflow/Task **worker** / sub-agent + background call | no interactive-only tool present | your **worker** model |

`main()` advertises a synthesized **`Worker → <name>`** entry (id
`claude-worker-<x>`, routed exactly like its base model) for every model in your
config. That gives you two ways to set the tiers:

- The launcher runs `scripts/ultracode_selector.py` before Claude Code starts. It
  reads `GET /uc/select`, lets you choose left-column orchestrator + right-column
  worker, then posts the choice back to `POST /uc/select`. The selector prints
  the orchestrator id on stdout so the launcher can pass it to `claude --model`.
- Inside Claude Code, `/model` still lists both plain model entries and
  `Worker → X` entries, so you can change either tier mid-session.

Selection rules:

- A **plain pick** (e.g. `claude-minimax-m3`) or selector worker value
  **`Same as orchestrator`** sets both tiers → that model runs everything.
- A **`Worker → X` pick** (or selector worker model) sets only the worker tier →
  orchestrator stays whatever you picked.
- **Stock ids** (`claude-opus-4-8`, sonnet, haiku — the workflow's hardcoded
  background traffic) never change the selection; they're **remapped** to it. That
  is what makes "use MiniMax" mean MiniMax for the whole workflow.

**Seeing the active tiers.** Claude Code's UI doesn't show orchestrator vs worker
separately. While the proxy is running:

- `ultracode status` (or `.\windows\Start-UltraCode.ps1 -Status` on Windows)
- `GET /healthz` → `orchestrator_worker`
- `GET /uc/select` → `active`

If a worker model hits a rate limit mid-task, pick **`Worker → <other>`** in
`/model` — only the worker tier changes. Role-targeted slash commands like
`/model worker` are not available (that's Claude Code's picker, not the proxy).

The selection lives in the proxy process (one `claude` session), guarded by a
lock, and resets when the proxy restarts. Disable tier routing with
`UC_ORCH_WORKER=0` (then a pick routes 1:1 and stock ids pass through untouched).
Disable only the pre-launch selector with `UC_SELECTOR=0`. Set `UC_TIER_LOG=1` to
log the per-request tier + remap.

**Parallelism.** The proxy is a `ThreadingHTTPServer`, so the N workers a workflow
fans out are handled concurrently — there's no artificial serialization in the
shim; throughput is bounded only by your backend's own rate limits.

## 5.5 Auto Router (pick the model per task)

A route of `type: "auto"` is not a backend — it's the **Auto Router**. When a
request resolves to the auto picker (`claude-auto`), the proxy:

1. Builds a compact task signal from the request (the latest non-tool user
   message, whether images are present, the tier/surface, turn count).
2. Sends a small, non-streaming scoring request to the **classifier** model named
   in `config.json → router.classifier` (one of your own cheap backends). The
   classifier prompt contains a short **capability card** for each candidate and
   asks for a `0.0–1.0` first-try success probability per candidate.
3. Parses the scores (clamped to `[0,1]`; any candidate that can't take images is
   hard-zeroed when the task has images), then selects the **cheapest candidate
   whose score ≥ `threshold`** (default `0.7`); if none clear it, the highest
   scorer wins.
4. Rewrites `body["model"]` to that candidate and dispatches through the normal
   route path (so tool translation, reliability, etc. all still apply).

The classifier is **never told the price** — cost only enters via the
"cheapest among those good enough" tie-break, so it can't be nudged toward
expensive models. The decision is **cached per task** (keyed on the user message
+ tier) so a task's tool-call round-trips don't re-pay the classifier. It runs in
both tiers: `Auto` as orchestrator routes the main loop; `Worker → Auto` routes
every sub-agent.

It fails safe at every step: a missing/again classifier or any error falls back
to `router.default` (or the cheapest candidate) deterministically; candidates
without a configured route are skipped; and if the router is disabled while
`claude-auto` is somehow picked, it's coerced to a real candidate so the synthetic
id is never sent upstream. Full reference: [AUTO_ROUTER.md](AUTO_ROUTER.md).

Router knobs:

| Env var | Default | What it does |
|---------|---------|--------------|
| `UC_ROUTER` | `1` | Master runtime switch; `0` disables even if enabled in config. |
| `UC_ROUTER_TIMEOUT` | `12` | Seconds to wait for the classifier before falling back. |
| `UC_ROUTER_MAX_TOKENS` | `600` | Cap on the classifier's reply (it only emits small JSON). |
| `UC_ROUTER_LOG` | `0` | Log each routing decision + raw scores. |

## 6. Reliability — surviving long and dynamic workflows

UltraCode's value is *long, autonomous* runs (deep reasoning, multi-step
Workflows, multi-agent fan-out). The weak point of any "translate to a third-party
backend" shim is that those backends occasionally hiccup — and on a 40-minute
agent run, one hiccup that isn't handled can wedge the whole session. The proxy
defends against the three failure modes we actually hit in production:

### a. Empty turns are auto-retried

Some upstreams intermittently return an assistant turn with **no text and no tool
call** — a transient blip, or a budget-exhausted `response.incomplete` reasoning
turn at high effort (notably GPT‑5.5 via codex). An empty turn is useless to
Claude Code and can stall a Workflow step. `_events_with_retry()` transparently
re-issues a fresh turn:

- It **buffers only until the first meaningful event**, so a normal turn adds
  **zero latency** and already-streamed output is never duplicated.
- It retries only on an empty/transient result — **never** after meaningful
  output, a fatal (non-retryable `4xx`) error, or partial output already streamed.
- Tunable: `UC_EMPTY_RETRY_ATTEMPTS` (default `2`), `UC_EMPTY_RETRY_BACKOFF`
  (default `0.75`s). Wraps both the `codex_oauth` and `openai_compat` paths.

### b. A stalled stream can't freeze the whole run

A codex stream can open (SSE established) and then go **silent mid-turn**.
Previously that blocked on the 600s socket read timeout — so a single hung
sub-agent could freeze an entire multi-agent / dynamic-workflow run for ~10
minutes. The codex reader now uses a **bounded per-read idle timeout**
(`UC_CODEX_STREAM_IDLE_TIMEOUT`, default `150`s): a stall becomes a *retryable*
error, the empty-turn retry above re-attempts, and the workflow keeps moving.
Lower it for faster recovery; raise it if your effort level legitimately produces
long silent reasoning gaps before the first token.

### c. Rejected / partial tool calls don't 400 strict backends

OpenAI's format requires every assistant `tool_calls` message to be *immediately*
followed by exactly one `tool` message per `tool_call_id`. When you **reject** a
tool call, Claude Code puts your comment in the same turn as (or instead of) the
tool result — which made strict backends like **DeepSeek** reject the next request
with *"insufficient tool messages following tool_calls message"* ([#3][i3]). The
translator now tracks the open tool-call ids, emits the tool replies **first** (in
order), **synthesizes a stub reply** for any call you didn't answer (rejected or
skipped — including partial *parallel* calls), then appends your comment. So
rejecting a tool mid-run just works, on every backend.

[i3]: https://github.com/OnlyTerp/UltraCode-Shim/issues/3

### d. No "dead air" while a reasoning model thinks

Reasoning models (MiniMax‑M3, DeepSeek‑R*, …) stream their chain-of-thought under
`reasoning_content` for several seconds *before* the first answer token. We keep
that thinking out of the visible answer — but a silent connection makes Claude
Code's workflow UI look frozen. The proxy now surfaces each reasoning chunk as a
keepalive on the stream, so the turn shows live activity while the model thinks,
without leaking the chain-of-thought into the reply.

### Reliability + workflow knobs

| Env var | Default | What it does |
|---------|---------|--------------|
| `UC_EMPTY_RETRY_ATTEMPTS` | `2` | How many times to re-issue an empty/transient turn. |
| `UC_EMPTY_RETRY_BACKOFF` | `0.75` | Seconds to wait between those retries. |
| `UC_CODEX_STREAM_IDLE_TIMEOUT` | `150` | Per-read idle cap (s) on the codex stream so a stall retries instead of hanging. |
| `UC_ORCH_WORKER` | `1` | Orchestrator+worker two-tier routing (§5). Set `0` to route each pick 1:1. |
| `UC_SELECTOR` | `1` | Pre-launch two-column selector. Set `0` to skip it and choose from `/model` only. |
| `UC_TIER_LOG` | `0` | Log the per-request tier (`heavy`/`fast`) + any model remap. |

These are covered by the offline self-test (`test_proxy.py`) so they don't
regress.

## 7. What touches your machine

- The launchers set env (`ANTHROPIC_BASE_URL`, discovery flag) for **the launched
  process only** and pass Claude Code a session-scoped `--settings` file. Your
  global `~/.claude` config and credentials are never modified.
- `config.json` (your keys/choices) is **gitignored**.
- The proxy is stopped when Claude Code exits.

## File map

| Path | What |
|------|------|
| `proxy.py` | the interceptor: envelope + `/v1/models` discovery + routing. Stdlib only. |
| `providers/codex_oauth.py` | optional GPT‑5.5-via-ChatGPT-login helper. Stdlib only. |
| `providers/grok_build.py` | optional xAI-Grok-via-Grok-login token helper. Stdlib only. |
| `providers/cursor_agent.py` | optional Cursor Composer bridge (experimental). Stdlib only. |
| `config.json` | your models + routes + keys (copied from `config.example.json`; gitignored). |
| `test_proxy.py` | offline end-to-end self-test (no network/keys). |
| `scripts/doctor.py` | environment + config validator that runs the self-test. |
| `windows/Start-UltraCode.ps1`, `bin/ultracode` | launchers (start proxy, run Claude Code, clean up). |
