<p align="center">
  <img src="../assets/brand/doc-trouble.png" alt="Troubleshooting — symptom to cause to fix, starting with the doctor" width="100%">
</p>

First, always run the doctor — it catches most problems and prints the fix:

```
python scripts/doctor.py        # mac/linux: python3
```

The proxy log is at:
- Windows: `%LOCALAPPDATA%\UltraCode-Shim\ultracode_proxy.log`
- mac/linux/WSL: `~/.local/state/ultracode-shim/proxy.log`

---

### My model doesn't appear in `/model`

- **Id doesn't start with `claude` or `anthropic`.** Claude Code filters
  discovered ids with `/^(claude|anthropic)/i`. Rename it (e.g. `claude-mimo`).
- **Discovery not enabled.** The launcher sets
  `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`; if you started `claude` yourself,
  set it (and `ANTHROPIC_BASE_URL`) or just use the launcher.
- **First open timing.** Discovery fetches in the background. Close `/model` and
  reopen, or restart Claude Code — the launcher pre-seeds the cache so it should
  show immediately.
- **You're not on an OAuth login.** Gateway discovery only triggers for
  first-party (OAuth) logins, not raw `ANTHROPIC_API_KEY` keys.

### Real Claude (Opus / Sonnet / Haiku) is missing from `/model`

You don't configure real Claude — the proxy always advertises the stock Claude
models so Opus/Sonnet/Haiku stay in the picker even when there's no Anthropic key
to list them upstream. If they're missing:

- **Stock models were turned off.** Check you didn't set
  `proxy.include_stock_models: false` in `config.json` or
  `UC_INCLUDE_STOCK_MODELS=0` in the environment. They're on by default.
- **A custom `UC_STOCK_MODELS` override.** If you set this env var, only the ids
  you listed are advertised (it wins over the learned + built-in lists). Unset it
  for the automatic list, or include the ids you want (e.g.
  `UC_STOCK_MODELS='claude-opus-4-8,claude-sonnet-4-6'`).
- **Stale discovery cache.** Close and reopen `/model`, or restart Claude Code —
  the launcher pre-seeds the cache with stock + your models on every launch.
- **Old version.** Earlier builds served custom-only when the upstream
  `/v1/models` fetch failed, so Opus could vanish. `git pull` (or re-run the
  installer) to get the always-on stock list.

**A brand-new Claude model isn't showing up?** The stock list self-updates: the
proxy learns real Claude ids from a **successful** upstream `/v1/models` fetch and
caches them, so a new Opus normally appears on its own. If it hasn't:

- **Learning is off or upstream hasn't been hit.** Learning needs at least one
  successful upstream fetch (a working Claude/OAuth login). Check
  `proxy.learn_stock_models` / `UC_STOCK_LEARN` aren't disabled; confirm on
  `/healthz` → `stock_learning` (`enabled`, `learned`, `cache`).
- **Need it right now without waiting.** List it explicitly via `UC_STOCK_MODELS`
  (e.g. `UC_STOCK_MODELS='claude-opus-5-0'`) — that's served immediately.
- **Reset the learned cache.** Delete the file shown at `/healthz` →
  `stock_learning.cache` and relaunch.

### A `/model` pick leaked into my global default (plain `claude` now errors on the model)

Claude Code persists an in-session `/model` pick (pressing Enter in the picker) to
your **user-global** settings (`~/.claude/settings.json`, the `model` key) as of
v2.1.153. Under UltraCode that means picking a proxy-only id (e.g.
`claude-composer`, `claude-gpt-5.5-codex`) becomes your global default — and a
plain `claude` run **outside** the proxy then fails, because the real Anthropic
API doesn't know that id.

The launchers guard against this: `bin/ultracode` (and
`windows\Start-UltraCode.ps1`) snapshot the `model` key before launch and restore
it on exit, so `/model` picks stay session-scoped. It's ref-count-safe across
concurrent sessions (the last one out restores) and only touches the `model` key —
the rest of `settings.json` is left intact.

- **Already polluted?** Set `"model"` in `~/.claude/settings.json` back to a real
  id (e.g. `"claude-opus-4-8"`), or run `/model` once in a plain `claude` session
  and pick a real Claude model.
- **Want a `/model` pick to persist for plain `claude` too?** Disable the guard
  with `UC_PRESERVE_GLOBAL_MODEL=0` before launching — in-session picks then save
  globally as Claude Code normally does.
- **Keep a pick for this session only without saving (even with the guard off):**
  press `s` in the `/model` picker instead of Enter.

### Which model is orchestrator vs worker right now?

Orchestrator/worker routing is sticky inside the proxy process, but Claude Code's
UI doesn't show the two tiers separately.

- **Quick status:** `ultracode status` (mac/Linux/WSL) or
  `.\windows\Start-UltraCode.ps1 -Status` (Windows). Shows the active
  orchestrator and worker ids + display names.
- **JSON:** `curl -s http://127.0.0.1:8141/healthz | python3 -m json.tool` →
  `orchestrator_worker`.
- **Also:** `curl -s http://127.0.0.1:8141/uc/select` returns the same
  `active` block while the proxy is running.

**Changing models mid-session:**

| What you pick in `/model` | What changes |
|---------------------------|--------------|
| A plain model (e.g. `claude-minimax-m3`) | **Both** orchestrator and worker → that model runs everything |
| `Worker → <model>` | **Worker only** — orchestrator stays as-is |
| Stock ids (`claude-opus-4-8`, sonnet, haiku) | **Neither tier** — they're remapped to your picks for background traffic |

**Worker hit a rate limit mid-task?** Open `/model`, pick `Worker → <other model>`.
The orchestrator tier is unchanged; only parallel workers/sub-agents switch.

**`/model orchestrator` / `/model worker`?** Not available — `/model` is Claude
Code's built-in picker; the proxy only sees the resulting model id on the next
request. Use the plain vs `Worker →` entries above.

### OpenAI-compat backend errors on long sessions (context length / 400)

The proxy forwards the **entire** Anthropic transcript to `openai_compat` backends
with no automatic trimming. On long multi-tool workflows a backend may return
`context length exceeded`, `maximum context`, or similar 400s.

- **First:** compact the session (`/compact` in Claude Code) or start a fresh
  session and carry over only what you need.
- **Switch worker only:** if the orchestrator is fine but workers are failing,
  `/model` → `Worker → <model with a larger window>`.
- **Proxy hint:** when the upstream error looks context-related, the proxy log
  and error message include a short note explaining that the full history was sent.

Strict backends also require `content: null` (not `""`) on tool-only assistant
turns; the proxy handles that automatically.

### The pre-launch selector doesn't open / says it cannot reach `/uc/select`

- **Proxy not healthy yet or wrong port.** The launcher starts the proxy before
  the selector. If the selector says it cannot reach `/uc/select`, check the
  proxy log path above and confirm `config.json`'s `proxy.listen_port` matches
  any `UC_LISTEN_PORT` override.
- **Non-interactive terminal.** The selector draws on the controlling terminal
  (`/dev/tty` or `CONOUT$`) while stdout is reserved for the selected model id.
  If your terminal runner has no TTY, set `UC_SELECTOR=0` and choose models from
  `/model` after Claude opens.
- **Need to bypass it.** Set `UC_SELECTOR=0` before launching. Orchestrator/worker
  routing still works through `/model` (`Worker → X` entries set the worker tier).

### The model is listed but the answer is an error / empty

- Check the proxy log (path above) for the upstream status line.
- **401 / "invalid api key":** the route's `auth` in `config.json` (or the
  `${VAR}` it references) is wrong/empty, or the header format is wrong. Re-run
  the doctor.
- **404 / "model not found":** the route's `model` isn't a valid id for that
  backend (remember: it's the backend's id, not the `claude-…` alias), or
  `upstream` is wrong.
- **Codex 401 / "run codex login":** your ChatGPT/Codex token expired — run
  `codex login` again.
- **Grok "grok_build auth" / 401:** the Grok login token could not be read or
  refreshed. Run `grok login --oauth` again (creates/updates `~/.grok/auth.json`).
  The proxy auto-refreshes the ~6h token, but a fully expired refresh token or a
  lapsed subscription needs a fresh login. `grok_build` routes need
  `providers/grok_build.py` present; if it 501s, the file is missing from the
  checkout.
- **Occasional empty reply:** some upstreams (notably GPT‑5.5 via codex at high
  effort, or a flaky OpenAI‑compatible backend) now and then return a turn with no
  text and no tool call. The proxy auto-retries a fresh turn (default 2 retries)
  before giving up, so this is usually invisible. Tune with `UC_EMPTY_RETRY_ATTEMPTS`
  and `UC_EMPTY_RETRY_BACKOFF`.
- **A turn hangs / "thinks" for many minutes (worst in multi-agent / dynamic
  workflows):** the codex upstream sometimes opens the stream and then goes silent
  mid-turn. The codex reader uses a bounded per-read idle timeout
  (`UC_CODEX_STREAM_IDLE_TIMEOUT`, default `150` seconds): a stall becomes a
  retryable error and the empty-turn retry re-attempts, instead of blocking on the
  old 10-minute socket timeout (which would freeze an entire workflow on a single
  hung sub-agent). Lower it for faster recovery; raise it if your effort level
  legitimately produces long silent reasoning gaps before the first token.

### It replies in text but never calls tools

The route is probably **passthrough** (or pointing at a chat endpoint that drops
tools). Set `"type": "openai_compat"` for that route — the proxy then translates
Anthropic `tool_use`/`tool_result` ⇄ OpenAI `tool_calls` both ways. Real Claude
(passthrough) already handles tools natively.

### OpenCode Go (Zen) returns 401 "Model … not supported" or 403 "error code: 1010"

OpenCode's **Go** subscription is an **OpenAI-compatible** API — not Anthropic — and
three config details trip people up:

- **Use `"type": "openai_compat"`**, with `upstream` ending in `/zen/go/v1` (the proxy
  appends `/chat/completions`). An Anthropic passthrough to `…/zen/go` won't work.
- **Model ids are bare** — `deepseek-v4-pro`, not `opencode-go/deepseek-v4-pro` (the
  `opencode-go/` prefix is the `opencode` CLI's namespace, not the API id). A wrong id
  returns `401 {"type":"ModelError","message":"Model … is not supported"}`.
- **Set a `User-Agent` header.** The endpoint is behind Cloudflare, which blocks the
  default client User-Agent with `403 error code: 1010`.

Two more gotchas: `https://opencode.ai/zen/v1` (no `/go`) is the separate
**pay-as-you-go** endpoint, not the subscription — an empty balance there shows as
`401 {"type":"CreditsError","message":"Insufficient balance"}`. And DeepSeek V4 are
reasoning models; the proxy already keeps their `reasoning_content` out of the visible
answer.

### Rejecting a tool call errors with "insufficient tool messages following tool_calls message"

Symptom (seen on strict backends like DeepSeek via OpenCode Zen):

```
openai_compat upstream 400: ... An assistant message with 'tool_calls' must be
followed by tool messages responding to each 'tool_call_id'.
```

When you **reject** (or skip) a tool call, Claude Code sends your typed comment in
the same user turn as the tool result — and sometimes sends **no** result at all.
OpenAI's format requires every assistant `tool_calls` message to be *immediately*
followed by exactly one `tool` message per `tool_call_id`; strict backends reject
anything else. The proxy now handles this for you: it emits the tool replies
first (in order), **synthesizes a stub reply** for any call you didn't answer
("Tool call was not executed…"), and puts your comment after. Same fix covers
parallel tool calls where you only answer some. Just update to the latest
`proxy.py` — no config change needed.

### The answer contains `<think>…</think>` reasoning (MiniMax‑M3 and other reasoning models)

The model is inlining its chain‑of‑thought into the visible reply. For
**MiniMax‑M3**, add `"body": { "reasoning_split": true }` to its `openai_compat`
route so the thinking is returned in a separate `reasoning_content` field instead
of being dumped into the answer:

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

The shipped `config.example.json` already sets this — if you wrote your own
`config.json`, copy the `body` line over. Other reasoning backends may expose a
similar flag under a different name; the generic `body` dict lets you pass
whatever request param that provider documents. See
[ADD_A_MODEL.md](ADD_A_MODEL.md#minimax-m3).

### Auto Router always picks the same model / isn't escalating

Run the proxy with `UC_ROUTER_LOG=1` and read the `[router] ... scores={...}`
lines in the log (paths above). Then:

- **Only one candidate is available.** Candidates whose `id` isn't a configured
  route are skipped; with one left, the router just uses it. Add more candidate
  routes. `GET /healthz` → `router.candidates` shows what's actually live.
- **Classifier can't run** → you'll see `no classifier` / `classifier failed` and
  a deterministic cheapest pick. Make sure `router.classifier` is a working route
  (test that model on its own first).
- **Cards are too vague.** The classifier routes off the `card` text. Spell out
  each candidate's strengths *and* weaknesses (see [AUTO_ROUTER.md](AUTO_ROUTER.md)).
- **Threshold too low/high.** Everything clears a low bar → always the cheapest;
  nothing clears a high bar → always the top scorer. Nudge `router.threshold`
  (0.7 is a good middle).
- **Caching looks "stuck".** The decision is cached per task (per user message);
  follow-up tool-call round-trips reuse it on purpose. A new instruction
  re-classifies.

### Auto Router picked a model but I wanted to choose manually

Pick any concrete model in `/model` (or the selector) instead of
`Auto (smart routing)`. To turn the feature off entirely, set
`router.enabled: false` in `config.json`, or launch with `UC_ROUTER=0`.

### "Auto (smart routing)" doesn't appear in `/model`

- `router.enabled` is `false`, or there's no `router` block.
- The `claude-auto` id was removed from `models`/`routes`. The proxy auto-creates
  them when `router.enabled` is true; re-run `python scripts/doctor.py` to confirm
  the router section is green.

### Composer (`cursor_agent`) hangs or times out

`cursor-agent` reaches Cursor's cloud on its own. If you're behind a
TLS-intercepting HTTP(S) proxy, it can hang until it times out. Set
`CURSOR_AGENT_NO_PROXY=1` (strips `HTTP(S)_PROXY`/`ALL_PROXY` for the cursor-agent
child) and confirm `cursor-agent login` succeeded. Composer is experimental:
plain answers and a best-effort tool bridge work, but it may not match your tool's
exact argument names.

### "Proxy did not become healthy"

- Another process is on the port. Pick another: set `proxy.listen_port` in
  `config.json` (or `UC_LISTEN_PORT`) and relaunch, or stop the stale
  `python … proxy.py`.
- Python not found. Ensure `python` (Windows) / `python3` (mac/linux) is on PATH.
- Run the proxy in the foreground to see the error:
  `python proxy.py` (Ctrl-C to stop).

### PowerShell won't run the script

```
powershell -ExecutionPolicy Bypass -File .\windows\Start-UltraCode.ps1
```

(The `irm ... | iex` install one-liner runs in your current session, so it isn't
blocked by execution policy. The same applies to a downloaded `install.ps1` you
dot-source; only saved `.ps1` files you invoke directly need the bypass.)

### `ultracode` isn't found after install

The installer puts the launcher in your bin dir (`~/.local/bin` on mac/linux,
`%LOCALAPPDATA%\Microsoft\WindowsApps` on Windows) and tells you if that dir
isn't on `PATH` yet. If `ultracode` isn't found:

- **Re-open your terminal** so a freshly-added PATH entry takes effect.
- **Add the bin dir to PATH** using the exact line the installer printed, or pass
  `--bin-dir` / `-BinDir` to install somewhere already on PATH.
- **Or just run it directly** — the installer prints the full path to the shim.
- **Re-point it** at a moved checkout by re-running the installer from the clone.

### The installer's self-test failed

The installer runs the offline self-test on **auto-picked free ports** before
installing. If it fails, the clone is broken (not your config) — re-run with
`--no-test` / `-NoTest` only to confirm the rest of the install, then run
`python3 test_proxy.py` and report the output. A `git pull` (or re-running the
installer) usually fixes a stale/partial checkout.

### Claude Code isn't found by the launcher

Install it (`npm i -g @anthropic-ai/claude-code`) and make sure `claude` is on
PATH (`claude --version`).

### Running on Windows but your project lives in WSL (split-brain setup)

If Claude Code runs as a Windows process (`claude.exe`) while your code, Python,
Node, and other tools live in WSL, the Bash tool is **Git Bash, not WSL** — so
`/home/...` paths fail with `Permission denied` and Linux tools look "not found".
The agent then flails (PowerShell workarounds, etc.). Two rules make tool calls
reliable:

- **Run Linux commands inside WSL**, keeping the whole command in the quoted
  `-lc '...'` string (this gives the full login PATH, makes `/home` work, and the
  output is captured back to Claude Code):

  ```
  wsl.exe -d <Distro> -e bash -lc 'cd /home/<you>/<project> && <command>'
  ```

  Don't pass a bare `/home/...` path as a *separate* argument to `wsl.exe` — Git
  Bash will mangle it. Keeping it inside the quoted `-lc` avoids that.

- **Create/edit files with Claude's Read/Write/Edit tools** using the
  `\\wsl.localhost\<Distro>\home\<you>\...` path. Those tools use the Windows
  filesystem API, so the UNC path works even though the Bash tool's `/home` does
  not.

Also note: WSL↔Windows `localhost` is only shared when WSL **mirrored networking**
is enabled; in the default NAT mode a service on one side is *not* reachable at
`127.0.0.1` from the other (so the proxy/shim and Claude Code must run on the same
side — here, all on Windows). The cleanest way to make any model follow the rules
above automatically is to drop them into a `CLAUDE.md` (project root, or the global
`~/.claude/CLAUDE.md`).

### Context fills up too fast / `/context` shows `200k` for a 1M model

**Symptom.** You picked Opus 4.8 (or Sonnet 4.6) — models with a 1M context
window — but the status-line meter climbs ~5× faster than expected and pins at
100%, auto-compaction fires early or not at all, and `/context` shows the limit
as `… / 200k` instead of `/ 1M`.

**Cause.** Claude Code only switches its context meter **and** auto-compaction to
the 1M window (and sends the `context-1m` beta) when the session's model id
carries the **`[1m]`** suffix (e.g. `claude-opus-4-8[1m]`). The selector and
`config.json` advertise *bare* ids (`claude-opus-4-8`), so the client defaults to
the 200k window even though Opus 4.8 / Opus 4.7 / Opus 4.6 / Sonnet 4.6 serve 1M
natively on the Anthropic API. Nothing is actually lost upstream — the window is
just mis-sized in the client.

**Fix.** Two parts work together. (1) The **launcher** appends `[1m]` to a
1M-capable Claude model chosen at launch. (2) The **proxy** also *advertises* the
`[1m]` suffix on `/v1/models` + `/healthz` for any **configured real-Claude
passthrough route** whose upstream model is 1M-capable (e.g. a `claude-opus` route
mapping to `claude-opus-4-8`) — so even an **in-session `/model` switch** (not just
the launch-time pick) gets the 1M window. The proxy strips the `[1m]` again before
routing, so it never reaches the backend. Relaunch, pick the model, and confirm
`/context` reads `/ 1M`.

- **Disable launcher suffixing** (back to bare ids): set `UC_FORCE_1M=0`.
- **Change the launcher's capable set:** set `UC_1M_MODELS` to a comma-separated
  list of base ids (default `claude-opus-4-8,claude-opus-4-7,claude-opus-4-6,claude-sonnet-4-6,claude-opus`).
- **Disable proxy advertising:** set `UC_ADVERTISE_1M=0`. Change which upstream
  models count as 1M with `UC_1M_UPSTREAM` (comma-separated upstream model ids;
  default the Opus 4.6–4.8 + Sonnet 4.6 family).
- **Not affected:** Haiku 4.5 (200k only), `claude-auto`, worker (`Worker → …`)
  entries, and non-Claude routes (Gemini / GPT / Composer) never get a `[1m]`
  suffix.
- **Caveat:** if your Anthropic-passthrough hop can fall back to a backend that
  only supports 200k, a conversation that grows past 200k may then fail there —
  make sure that fallback also honors 1M.

### Did I break my normal Claude Code?

No — this project never edits your global `~/.claude` config or credentials; it
only sets env for the launched process and uses a session `--settings` file. Use
the **"Claude Code (Normal)"** icon for the stock experience, or
`windows\Uninstall.ps1` to remove the shim entirely.

### Prove the install itself is fine

Run the offline self-test (no network, no keys):

```
python test_proxy.py
```

If that passes, the code is good and the problem is configuration/credentials. If
it fails, your clone is broken — re-clone and report the output.
