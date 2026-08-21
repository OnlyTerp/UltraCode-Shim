<p align="center">
  <img src="../assets/brand/doc-add.png" alt="Add a model — put any backend in the /model menu with templates per route type" width="100%">
</p>

Everything lives in one file: **`config.json`** (copied from
`config.example.json` on first run). To add a backend you edit two sections:

1. **`models`** — what appears in Claude Code's `/model` picker.
2. **`routes`** — where each of those ids actually goes.

```jsonc
{
  "models": [
    { "id": "claude-mimo", "display_name": "MiMo v2.5 Pro" }
  ],
  "routes": {
    "claude-mimo": {
      "type": "openai_compat",
      "upstream": "https://token-plan-sgp.xiaomimimo.com/v1",
      "model": "mimo-v2.5-pro",
      "auth": "Bearer ${MIMO_API_KEY}"
    }
  }
}
```

> **Two rules you must follow:**
> 1. Every `id` in `models` **must start with `claude` or `anthropic`** — Claude
>    Code drops everything else from `/model`.
> 2. The `id` in `models` must **exactly equal** the key in `routes`. Otherwise
>    the model won't appear or won't route.
>
> Run `python scripts/doctor.py` and it checks both for you.

## Where keys go

`config.json` is gitignored, so you can put a key **inline**:

```json
"auth": "Bearer sk-...your-real-key..."
```

…or keep it out of the file with **`${ENV}`** expansion:

```json
"auth": "Bearer ${MIMO_API_KEY}"
```

`${VAR}` is read from your environment. The launchers also load an optional
gitignored **`ultracode.env`** in the repo root, so you can keep keys there:

```
MIMO_API_KEY=...
OPENROUTER_API_KEY=...
```

## Route types

### `openai_compat` — anything that speaks OpenAI Chat Completions

MiMo, DeepSeek, StepFun, Ollama Cloud, OpenRouter, OpenAI, Together, a local
llama.cpp / LM Studio server, etc. Tool calls are translated both ways.

```json
"claude-openrouter": {
  "type": "openai_compat",
  "upstream": "https://openrouter.ai/api/v1",
  "model": "meta-llama/llama-3.3-70b-instruct",
  "auth": "Bearer ${OPENROUTER_API_KEY}"
}
```

[Requesty](https://requesty.ai) is another OpenAI-compatible gateway — same
shape, just a different `upstream` and `provider/model` naming
([docs](https://docs.requesty.ai), keys at
[app.requesty.ai/api-keys](https://app.requesty.ai/api-keys)):

```json
"claude-requesty": {
  "type": "openai_compat",
  "upstream": "https://router.requesty.ai/v1",
  "model": "openai/gpt-4o-mini",
  "auth": "Bearer ${REQUESTY_API_KEY}"
}
```


- `upstream` is the OpenAI **base URL exactly as the provider documents it**
  (usually ends in `/v1`). The proxy appends `/chat/completions`.
- `model` is the backend's real model id, **not** the `claude-…` alias.
- Optional: `headers` (a dict, values support `${VARS}`), `max_output_tokens`
  (completion cap, default 8192 — raise it if a backend supports longer output),
  and `body` (a dict merged into every request body, values support `${VARS}`) —
  for provider-specific flags like MiniMax‑M3's `reasoning_split` (see below).

A **local** server is the same, with a usually-ignored key:

```json
"claude-local": {
  "type": "openai_compat",
  "upstream": "http://127.0.0.1:11434/v1",
  "model": "your-local-model",
  "auth": "Bearer local"
}
```

### MiniMax‑M3

MiniMax‑M3 is OpenAI‑compatible (it's the `openai_compat` type), but it has one
gotcha worth calling out:

```json
"claude-minimax-m3": {
  "type": "openai_compat",
  "upstream": "https://api.minimax.io/v1",
  "model": "MiniMax-M3",
  "auth": "Bearer ${MINIMAX_API_KEY}",
  "max_output_tokens": 64000,
  "body": { "reasoning_split": true }
}
```

- **Get a key** at [platform.minimax.io](https://platform.minimax.io) → put it
  inline or set `MINIMAX_API_KEY` (env or `ultracode.env`).
- **`"body": { "reasoning_split": true }` is the important part.** M3 is a
  reasoning model: by default it streams its chain‑of‑thought **inline** as
  `<think>…</think>` right inside the answer, which clutters Claude Code's output
  and confuses tool parsing. With `reasoning_split` on, the thinking is returned
  in a separate `reasoning_content` field, so the visible answer stays clean.
  Leave it out and you'll see raw `<think>` blocks in replies.
- `model` is `MiniMax-M3` (capitalized exactly like that).
- `max_output_tokens` can go up to **64000**; M3's context is ~1M tokens.
- The `body` dict is generic — any `openai_compat` backend can use it to pass
  provider‑specific request params (values support `${VARS}`).

The shipped `config.example.json` already includes this entry — just add your key.

### `openai_compat` — OpenCode Go (OpenCode Zen subscription)

```json
"claude-opencode": {
  "type": "openai_compat",
  "upstream": "https://opencode.ai/zen/go/v1",
  "model": "deepseek-v4-pro",
  "auth": "Bearer ${OPENCODE_API_KEY}",
  "headers": { "User-Agent": "openclaw/2026.4.20" }
}
```

- It's an **OpenAI-compatible** API, *not* Anthropic. The base URL ends in
  `/zen/go/v1`; the proxy appends `/chat/completions`.
- **Model ids are bare** — `deepseek-v4-pro`, `deepseek-v4-flash`, `kimi-k2.6`,
  `glm-5.1`, `minimax-m3`, … — *not* the `opencode-go/`-prefixed ids the
  `opencode` CLI prints (that prefix is the CLI's provider namespace, not the
  API id). A wrong id returns `401 {"type":"ModelError","message":"Model … is not supported"}`.
- A **`User-Agent` header is required**: the endpoint is behind Cloudflare, which
  rejects the default client UA with `403 error code: 1010`.
- `https://opencode.ai/zen/v1` (no `/go`) is the separate **pay-as-you-go**
  endpoint; `…/zen/go/v1` is the **subscription**. An empty PAYG balance shows as
  `401 {"type":"CreditsError","message":"Insufficient balance"}`.
- DeepSeek V4 models are reasoning models — their chain-of-thought returns as
  `reasoning_content`, which the proxy keeps out of the visible answer.

### Anthropic passthrough — real Claude or an Anthropic-compatible endpoint

Omit `type` (or set `"anthropic"`). With no `auth`/`upstream` it's just real
Claude with the UltraCode envelope. You can also point at another
Anthropic-shaped gateway and add headers:

```json
"claude-gateway": {
  "upstream": "https://your-anthropic-gateway.example.com",
  "model": "claude-sonnet-4-5",
  "auth": "Bearer ${GATEWAY_API_KEY}"
}
```

### `codex_oauth` — GPT‑5.5 via a ChatGPT/Codex login (no API key)

```json
"claude-gpt-5.5-codex": { "type": "codex_oauth", "model": "gpt-5.5" }
```

Run `codex login` once (creates `~/.codex/auth.json`). No `auth`/`upstream`
needed. Optional env knobs: `UC_CODEX_EFFORT`, `UC_CODEX_SERVICE_TIER`,
`CODEX_HOME`.

### `grok_build` — xAI Grok via a Grok login (no API key)

```json
"claude-grok-4-6": { "type": "grok_build", "model": "grok-4.6", "max_output_tokens": 16384 }
```

Uses your **Grok subscription** (SuperGrok / X Premium+), not a metered xAI API
key. Run `grok login --oauth` once — or `grok login --device-auth` on a headless
box — with the official Grok CLI (creates `~/.grok/auth.json`). No `auth` or
`upstream` needed and both are **ignored on purpose**: the proxy selects the
subscription (OIDC) credential, refreshes it against `auth.x.ai` when it nears its
~6h expiry (serialized across the proxy's handler threads so the rotating refresh
token is never double-spent), and routes to the **pinned** endpoint the official
Grok CLI uses for session inference, `https://cli-chat-proxy.grok.com/v1`, with
the required CLI session headers (`X-XAI-Token-Auth`, `x-grok-client-version`,
`x-grok-model-override`, …). Pinning keeps the implicitly-loaded login token from
ever being sent to another host. Plain OpenAI Chat Completions, so tool-calling
and streaming work as usual. There is **no** fallback to `api.x.ai` (xAI's own
resolver labels it the API-key surface) — if the session proxy rejects the request
(e.g. an out-of-date CLI: run `grok update`), the route errors rather than silently
switching surfaces. For pay-per-token API-key access, add a separate
`openai_compat` route with an `XAI_API_KEY` instead.

`model` is xAI's real id: `grok-4.6`, `grok-4.5`, `grok-4.3`, the `grok-4.20`
variants, etc. Grok returns its chain-of-thought in a separate `reasoning_content`
field, so — unlike MiniMax-M3 — you do **not** need `body.reasoning_split`.
Optional env knobs: `GROK_HOME` (default `~/.grok`), `UC_GROK_BASE_URL`,
`UC_GROK_TOKEN_URL`, `UC_GROK_REFRESH_SKEW` (seconds before expiry, default 120),
`UC_GROK_CLIENT_VERSION` (override the CLI version header).

### `cursor_agent` — Cursor Composer (experimental)

```json
"claude-composer": { "type": "cursor_agent", "model": "composer-2.5" }
```

Needs the `cursor-agent` CLI and `cursor-agent login`. It's an autonomous agent,
not a plain endpoint, so we run it in read-only "ask" mode and bridge tool calls
as text markers — great for reasoning/answers, best-effort for tool-calling (the
model may not match your tool's exact argument names).

Live-tested: plain answers and the tool bridge both work (~4–7s per turn). Knobs:
`CURSOR_AGENT_TIMEOUT` (default 240s) and `CURSOR_AGENT_WORKSPACE`.

> **Behind an intercepting HTTP(S) proxy?** `cursor-agent` talks to Cursor's
> cloud itself; a TLS-intercepting proxy can make it hang/time out. Set
> `CURSOR_AGENT_NO_PROXY=1` to strip `HTTP(S)_PROXY`/`ALL_PROXY` from the
> cursor-agent child process.

## What ships in the example

`config.example.json` includes ready-to-use entries — keep the ones you have a
plan/key for and delete the rest:

| Picker label | id | `type` | backend |
|--------------|----|--------|---------|
| GPT-5.5 (Codex OAuth) | `claude-gpt-5.5-codex` | `codex_oauth` | `codex login` |
| Grok 4.6 (xAI login) | `claude-grok-4-6` | `grok_build` | `grok login --oauth` |
| MiniMax-M3 | `claude-minimax-m3` | `openai_compat` | MiniMax (`reasoning_split`) |
| MiMo v2.5 Pro | `claude-mimo` | `openai_compat` | Xiaomi MiMo |
| DeepSeek V4 Pro/Flash | `claude-deepseek-v4-*` | `openai_compat` | DeepSeek |
| Step Flash | `claude-step-flash` | `openai_compat` | StepFun |
| Ollama Cloud | `claude-ollama-cloud` | `openai_compat` | Ollama Cloud |
| DeepSeek V4 Pro (OpenCode Go) | `claude-opencode` | `openai_compat` | OpenCode Zen (Go subscription) |
| Llama 3.3 70B (OpenRouter) | `claude-openrouter` | `openai_compat` | OpenRouter |
| GPT-4o mini (Requesty) | `claude-requesty` | `openai_compat` | Requesty |
| Local model | `claude-local` | `openai_compat` | local server |
| Composer 2.5 (experimental) | `claude-composer` | `cursor_agent` | cursor-agent |
| Auto (smart routing) | `claude-auto` | `auto` | picks among your backends per task |

## Make a model a candidate for the Auto Router

Adding a model to the `/model` menu (above) is independent of the
[Auto Router](AUTO_ROUTER.md). To let the router *choose* a model automatically,
also list it under `router.candidates` with a relative `cost`, an
`supports_images` flag, and an honest capability `card`:

```jsonc
"router": {
  "enabled": true,
  "classifier": "claude-mimo",
  "candidates": [
    { "id": "claude-minimax-m3", "cost": 0.3, "supports_images": false,
      "card": "cheap, fast; single-file edits, codegen, simple refactors; weak on big refactors/debugging" },
    { "id": "claude-mimo",       "cost": 1.0, "supports_images": false,
      "card": "cheap generalist; standard infra/CRUD, data processing, moderate multi-file edits" }
  ]
}
```

The candidate `id` must match a route. Candidates without a route are skipped, so
the router keeps working with whatever subset you keep. Full reference:
[AUTO_ROUTER.md](AUTO_ROUTER.md).

After editing, validate and launch:

```
python scripts/doctor.py
windows\Start-UltraCode.ps1      # or  ./bin/ultracode  on mac/linux
```
