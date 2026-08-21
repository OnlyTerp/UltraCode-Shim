# Runbook for AI assistants

You are helping a user set up **UltraCode-Shim**: it lets Claude Code's UltraCode
mode run on any model the user already pays for, chosen from the pre-launch
orchestrator/worker selector or from the `/model` menu.

Follow these phases in order. Do not skip the doctor. Everything here is pure
Python standard library — **do not** `pip install` anything. Prefer the commands
exactly as written; they are cross-platform-aware.

## Phase 0 — Understand the goal (read once)

The end state: the user double-clicks **"UltraCode (All Models)"**, picks an
orchestrator + worker in the selector, and Claude Code opens already pointed at
that choice. They can still type `/model` later and pick any backend they
configured. The mechanism (no magic): a tiny loopback proxy (`proxy.py`) sits at
`ANTHROPIC_BASE_URL`, advertises the user's models plus synthesized `Worker → X`
entries on `GET /v1/models`, adds the UltraCode envelope to every request, and
routes each pick to its real backend. See `docs/HOW_IT_WORKS.md`.

## Phase 1 — Prerequisites

Run and confirm each:

1. `python3 --version` (or `python --version`) → must be ≥ 3.8.
2. `claude --version` → Claude Code CLI must be installed. If missing:
   `npm i -g @anthropic-ai/claude-code`.
3. Confirm the user has UltraCode access in their Claude plan (they'd have used
   `/effort ultracode` before). If not, this project can't grant it.

## Phase 2 — Get the code + baseline check

Fastest path — the one-command installer (clones if needed, runs the offline
self-test, creates `config.json`, and installs a `ultracode` launcher on PATH):

```
# mac/linux/WSL
curl -fsSL https://raw.githubusercontent.com/OnlyTerp/UltraCode-Shim/main/install.sh | bash
# windows (PowerShell)
irm https://raw.githubusercontent.com/OnlyTerp/UltraCode-Shim/main/install.ps1 | iex
```

If you already have a checkout, run `./install.sh` (or `.\install.ps1`) from
inside it. Either way, the installer runs the **offline self-test** for you.

If you'd rather do it by hand (or the installer can't run):

1. Clone if not already: `git clone https://github.com/OnlyTerp/UltraCode-Shim.git`
2. From the repo root run the doctor:
   ```
   python3 scripts/doctor.py
   ```

Either way, the **offline self-test** (no network/keys) proves the proxy,
discovery, the UltraCode envelope, and tool-call translation all work. If it
fails, STOP and report the output — the install is broken, not the user's config.

## Phase 3 — Ask what they have, then configure

Ask the user which of these they have (only configure those):

- An **API key** for an OpenAI-compatible service (MiniMax‑M3, MiMo, DeepSeek,
  OpenRouter, OpenAI, Ollama, a local llama.cpp/LM Studio server, etc.) → use
  `openai_compat`.
- A **ChatGPT/Codex login** for GPT‑5.5 → use `codex_oauth` (run `codex login`).
- A **Grok subscription** (SuperGrok / X Premium+) with the official Grok CLI →
  use `grok_build` (run `grok login --oauth`, or `grok login --device-auth` on a
  headless box). No API key: the proxy reads `~/.grok/auth.json`, refreshes the
  token itself, and calls `https://api.x.ai/v1`. See `docs/ADD_A_MODEL.md`.
- Just **Claude** → they can still use it, routed as Anthropic passthrough. No
  savings, but UltraCode works. **You don't need to configure real Claude at
  all:** the proxy always advertises the stock Claude models (Opus/Sonnet/Haiku)
  in `/model` and keeps them there even with no Anthropic key. The stock list is
  self-updating — the proxy learns real Claude ids from any successful upstream
  `/v1/models` fetch and caches them, so a newly released Opus appears on its own.
  So the user always has real (and current) Claude available; they only *add*
  cheaper backends. (Disable with `proxy.include_stock_models: false` /
  `UC_INCLUDE_STOCK_MODELS=0` for only-configured models; disable just the
  learning with `proxy.learn_stock_models: false` / `UC_STOCK_LEARN=0`.)

Then edit **one file**: copy `config.example.json` → `config.json` (the installer
and launcher also do this on first run), and edit `config.json`:

- `models` — one entry per model to show in `/model`. **Every `id` MUST start
  with `claude` or `anthropic`** or Claude Code drops it.
- `routes` — a route for each of those ids (the key must equal the `id`).
- Keys go inline (config.json is gitignored) or as `${VAR}` (export it, or use a
  gitignored `ultracode.env` in the repo root that the launchers load).

See `docs/ADD_A_MODEL.md` for exact templates per backend type.

Rules you must enforce:
- The `id` in `models` and the **key** in `routes` must be identical.
- For `openai_compat`, `model` is the backend's real model id (not the `claude-…` alias).
- For `openai_compat`, `upstream` is the provider's base URL (usually ends in
  `/v1`); the proxy appends `/chat/completions`.
- **Reasoning models that inline `<think>` (e.g. MiniMax‑M3):** add
  `"body": { "reasoning_split": true }` to the route so the chain‑of‑thought is
  split out of the visible answer. The shipped example already does this for
  `claude-minimax-m3`. The `body` dict is the general way to pass any
  provider‑specific request param.

## Phase 3.5 — (Optional) Auto Router

If the user wants UltraCode to **pick the model per task automatically** (cheap
model for trivial turns, strong model for hard ones), configure the `router`
block in `config.json`. It's already present and enabled in `config.example.json`
— you mainly prune it to the models they kept. Full reference:
`docs/AUTO_ROUTER.md`.

Rules you must enforce:
- `router.id` (default `claude-auto`) MUST also appear in `models` and have a
  `{"type":"auto"}` route. (The example already includes all three; the proxy
  also auto-creates them if `router.enabled` and they're missing.)
- Every `candidates[].id` and `router.classifier` MUST be a key in `routes`.
  Candidates without a route are silently skipped, so prune the candidate list to
  match the routes the user kept.
- `router.classifier` should be the **cheapest, fastest** model they configured
  (it runs on every new task). If it's unavailable the router still works — it
  just falls back to the cheapest candidate without scoring.
- `candidates[].cost` is a **relative** weight (ordering only). Order them
  cheap→expensive.
- Set `candidates[].supports_images` truthfully; image tasks skip models that
  can't see.
- Write an honest `candidates[].card` (strengths AND weaknesses) — that text is
  literally what the classifier reads to route. Vague cards → vague routing.
- If the user does NOT want auto routing, set `"enabled": false` (or delete the
  `router` block). The `claude-auto` model just won't appear.

The doctor (next phase) validates all of this.

## Phase 4 — Validate the real config

Run the doctor again:
```
python3 scripts/doctor.py
```
Now it validates the user's actual `config.json`: ids are discoverable+routed,
every `${VAR}` referenced by a route is present (or the key is inline), and
`codex_oauth`/`cursor_agent` routes have their login/CLI. Fix every `[FAIL]`
(each prints its fix) until exit code 0.

## Phase 5 — Launch

If you used the installer, just run `ultracode` (it's on PATH). Otherwise launch
from the checkout:

- **macOS/Linux/WSL:** `./bin/ultracode`
- **Windows:** `./windows/Start-UltraCode.ps1` (or `./install.ps1 -DesktopIcons`
  for "UltraCode (All Models)" + "Claude Code (Normal)" Desktop shortcuts).

The launcher starts the proxy, seeds Claude Code's gateway-models cache from the
live proxy (the stock Claude models + the user's configured models + synthesized
`Worker → X` entries), opens the two-column selector, then passes the selected
orchestrator as `claude --model ...`. Set `UC_SELECTOR=0` to skip the selector and
choose from `/model` only.

## Phase 6 — Verify end to end

1. Launch UltraCode. Confirm the selector appears and can pick an orchestrator +
   worker (`Same as orchestrator` means one model runs everything).
2. In Claude Code, type `/model` and confirm BOTH appear: the stock Claude models
   (Opus/Sonnet/Haiku) **and** the user's custom models + `Worker → X` entries
   (the proxy serves all of them on `GET /v1/models`).
3. Send a trivial prompt ("say OK"). Confirm a reply uses the selected model.
4. Pick one that needs tools and ask something requiring a tool call; confirm tools
   fire (the proxy translates tool calls both ways).
5. If you configured the Auto Router: pick **`Auto (smart routing)`**, run the
   proxy with `UC_ROUTER_LOG=1`, send a trivial prompt then a hard one, and
   confirm the proxy log shows a `[router] ... -> <model>` line choosing a cheap
   model for the trivial turn and a stronger one for the hard turn.

If a model doesn't appear or errors, go to `docs/TROUBLESHOOTING.md` and match the
symptom. Common ones:
- Model missing from `/model` → id didn't start with `claude`/`anthropic`, or
  discovery env not set (the launcher sets `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`).
- Real Claude (Opus/etc.) missing from `/model` → only if stock models were turned
  off (`proxy.include_stock_models: false` / `UC_INCLUDE_STOCK_MODELS=0`); they're
  on by default.
- "responded but never called tools" → that route must be `openai_compat` (not
  passthrough) so tools are translated.
- 401/empty from a backend → wrong/empty key in `config.json`, or expired
  `codex login`.

## Hard rules

- Never commit `config.json` or `ultracode.env` (they're gitignored; they hold
  the user's choices/keys).
- An API key may go inline in `config.json` (gitignored) or as `${VAR}`; never
  commit a real key.
- Don't modify the user's global `~/.claude` config; this tool is session-scoped.
- If the offline self-test (`python3 test_proxy.py`) fails, the problem is the
  code/clone, not the user — report it, don't paper over it.
