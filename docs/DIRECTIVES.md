# Routing directives — pin any request to a specific model

Routing directives ("pins") let a request's **prompt** force which backend serves
it, overriding the orchestrator/worker selection **and** the [Auto Router](AUTO_ROUTER.md).
They exist for one job in particular: making an **automated multi-agent workflow**
land each spawned sub-agent on the right model **by role** — e.g.

> **opus** writes the plan → **composer** writes the code → **codex** adversarially
> reviews it → **claude** fixes what the review got right.

You don't drive that turn-by-turn. The workflow script bakes a role tag into each
`agent()` prompt; the proxy reads the tag, hard-pins that request, strips the tag,
and forwards the rest. No tag → nothing changes, the normal routing flow decides.

It's **opt-in — OFF by default** (enable with `"directives": {"enabled": true}`
in `config.json`, or `UC_DIRECTIVES=1`), so pulling this feature never changes an
existing setup until you ask for it. Once on, it's fully local and degrades
safely: an unknown name, two names in one message, or a name that maps to the
synthetic `auto` route are all ignored, so a request is never broken.

---

## How a request gets pinned (30 seconds)

On every request the proxy looks at the **latest real user turn** (tool-result-only
turns are skipped, so a sub-agent's tag stays sticky across its tool calls) and
scans for a marker, most-explicit tier first. A tier wins only if it resolves to
**exactly one** configured backend:

| Tier | Form | Example | Stripped before forwarding? |
|------|------|---------|------------------------------|
| 1. Sentinel | `[[route:NAME]]` | `[[route:codex]] review this diff` | yes |
| 2. Tag | `@NAME` · `use:NAME` · `route:NAME` · `model:NAME` | `@composer implement the parser` | yes |
| 3. Natural language **(opt-in, off by default)** | `use/have/ask/let/with/via NAME` | `please have codex review it` | no (it's prose) |

> The natural-language tier is **off by default** — enable it with
> `UC_DIRECTIVES_NL=1`. It's deliberately opt-in because ordinary prose that merely
> mentions a model name after a trigger word (e.g. "*does this work **with Claude**?*")
> would otherwise silently reroute the request. With it off, only the explicit
> sentinel/tag forms pin.

`NAME` is resolved through an **alias table** (below). If it resolves to one
backend, that request is pinned there — skipping both the worker/orchestrator pick
and the Auto Router. If it resolves to **two or more** distinct backends (e.g. you
wrote `@opus then @composer`), that's ambiguous → ignored. If it resolves to
nothing, → ignored.

> Earlier picks: this implements **"explicit tag wins"** + **hard pin**. The tag
> is authoritative; natural language is only a fallback when no tag is present.

Watch decisions with `UC_DIRECTIVES_LOG=1`:

```
directive pin: tier=fast claude-deepseek-v4-flash -> claude-composer
[directive] ambiguous (claude-composer, claude-gpt-5.5-codex named); ignored
```

---

## Names (the alias table)

Names **auto-derive** from your configured model ids and display names, so the
obvious ones already work with no setup:

| You type | Resolves to (in the shipped config) |
|----------|--------------------------------------|
| `opus`, `claude` | `claude-opus` |
| `composer` | `claude-composer` |
| `codex` | `claude-gpt-5.5-codex` |
| `minimax` | `claude-minimax-m3` |
| `mimo` | `claude-mimo` |
| `deepseek-v4-pro`, `deepseek-v4-flash` | the matching route |

Matching is case- and punctuation-insensitive (`GPT-5.5`, `gpt5.5`, `gpt_5_5` all
collapse to the same key). A name that would map to **two** routes is dropped as
ambiguous — use the specific id, or pin it explicitly in `aliases`. In the shipped
example two such names are dropped: bare **`deepseek`** (matches both v4-pro and
v4-flash) and **`gpt`** (matches both `claude-gpt-5.5-codex` via `gpt-5.5` *and*
`claude-ollama-cloud` via its `gpt-oss` model) — so use `codex` for GPT-5.5, not
`gpt`.

### Configure it (optional)

The `directives` block in `config.json` (already present in the shipped config):

```jsonc
"directives": {
  "enabled": true,            // OFF by default — set true to turn the feature on
  // Friendly name -> route id. The common names (opus, claude, composer, codex,
  // minimax, mimo, ...) AUTO-DERIVE from your models, so list entries here only to
  // ADD a custom name or disambiguate one. RHS must be a route id.
  "aliases": {
    "fixer": "claude-opus",                 // a custom role name -> a route
    "deepseek": "claude-deepseek-v4-pro"    // disambiguate a name that maps to two routes
  },
  // Optional: interactive plan-mode turns with NO explicit pin auto-route here.
  "planner": "claude-opus",
  // Strip the marker from the prompt before forwarding (recommended).
  "strip": true
}
```

| Field | Meaning |
|-------|---------|
| `enabled` | Turn directives on/off. **Defaults to off.** An explicit `UC_DIRECTIVES` env var (1/0) overrides this either way. |
| `aliases` | `name → route id` overrides on top of the auto-derived table. The right side must be a route id in `routes`. |
| `planner` | If set, **plan-mode** turns (the interactive planning loop, detected structurally via the `ExitPlanMode` tool) with no explicit pin route here. Set `null` to disable. |
| `strip` | Remove the matched marker from the prompt before forwarding so the backend never sees it. |

### Knobs

| Env var | Default | Effect |
|---------|---------|--------|
| `UC_DIRECTIVES` | unset | `1` force-enables, `0` force-disables — overrides `directives.enabled`. Unset → follow config (default off). |
| `UC_DIRECTIVES_NL` | `0` | `1` enables the natural-language tier. **Off by default** (avoids prose like "with Claude" silently rerouting); only sentinel/tag pins count unless enabled. |
| `UC_DIRECTIVES_LOG` | `0` | `1` logs every pin / ambiguity / ignore decision. |

---

## The point: automated multi-agent workflows

Because a workflow script is something you (or the orchestrator) author, you don't
need any fuzzy "which phase is this?" inference — you state the role **in the
prompt**, deterministically:

```js
const plan   = await agent(`[[route:opus]] Write a plan for: ${task}`)
const code   = await agent(`[[route:composer]] Implement this plan:\n${plan}`)
const review = await agent(`[[route:codex]] Adversarially review:\n${code}`, { schema: REVIEW })
const fixed  = await agent(`[[route:claude]] Fix the valid issues:\n${JSON.stringify(review)}`)
```

Each `agent()` is a separate request; the proxy pins each one independently, so
every spawned sub-agent lands exactly where you declared — regardless of which
single worker model is selected in `/model`.

A complete, runnable version (with a structured review verdict and a conditional
fix step) ships at [`examples/role_pipeline_workflow.js`](../examples/role_pipeline_workflow.js).
Save it as `.claude/workflows/role-pipeline.js` to invoke by name, and pass the
task via `args`.

---

## Failure behavior (never breaks a request)

| Situation | What happens |
|-----------|--------------|
| No marker in the prompt | Normal routing (tier/worker selection, then Auto Router). |
| Name resolves to nothing | Ignored; normal routing. |
| Two+ distinct names in one turn | Ambiguous → ignored; normal routing. |
| Name maps to the `auto` route | Not a real backend → ignored. |
| Pinned backend errors at dispatch | Same handling as any other route (the pin only chooses *where*, not *how*). |
