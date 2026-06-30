#!/usr/bin/env python3
"""
proxy.py -- Anthropic-API interceptor that gives Claude Code's
"UltraCode" behavior to ANY model and lets you pick those models from the
/model menu.

WHAT IT DOES
------------
Claude Code talks to ANTHROPIC_BASE_URL. Point that at this proxy and it:

  1. Forces the UltraCode envelope on every /v1/messages request:
        output_config.effort = "xhigh"
      + thinking            = {"type": "adaptive"}
      + max_tokens          >= UC_MAX_TOKENS (default 64000)
      + an injected "Ultracode is on" system reminder
     (Per the reverse-engineering in docs/HOW_IT_WORKS.md, that *is* what
     UltraCode is at the API boundary -- there is no secret model or field.)

  2. Serves GET /v1/models, merging the real Anthropic list with your own
     custom models (config.json "models") AND a built-in set of stock Anthropic
     models (real Claude -- Opus/Sonnet/Haiku). The stock set is always offered
     so real Claude never disappears from /model, even when the upstream
     /v1/models fetch can't run (no Anthropic credential to forward, offline,
     etc.). With Claude Code's gateway model discovery enabled
     (CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1) those models appear in the
     /model picker. NOTE: Claude Code only keeps model ids matching
     /^(claude|anthropic)/i, so every custom id MUST start with "claude" or
     "anthropic".

  3. Routes each model id Claude Code sends to a real backend (config.json "routes"):
        - Anthropic passthrough  (real Claude, or any Anthropic endpoint)
        - openai_compat          (any OpenAI-compatible Chat Completions API,
                                   WITH full tool-calling translation)
        - codex_oauth            (GPT-5.5 via a ChatGPT/Codex login; needs the
                                   optional providers/codex_oauth.py)

It is dependency-light: Python 3 standard library only. No pip install.

ENV KNOBS
---------
  UC_LISTEN_HOST     default 127.0.0.1
  UC_LISTEN_PORT     default 8141
  UC_UPSTREAM        default https://api.anthropic.com
  UC_MAX_TOKENS      default 64000   (floor applied to max_tokens)
  UC_FORCE_EFFORT    default xhigh   (set empty to leave effort untouched)
  UC_FORCE_THINKING  default 1       (1 => force adaptive thinking)
  UC_INJECT_REMINDER default 1       (1 => inject the ultracode reminder)
  UC_INCLUDE_STOCK_MODELS default 1  (1 => always advertise stock Claude models
                     -- Opus/Sonnet/Haiku -- on /v1/models so real Claude never
                     drops out of the picker; 0 to advertise only your config)
  UC_STOCK_LEARN     default 1       (1 => learn the real Claude model ids from
                     any successful upstream /v1/models fetch and cache them to
                     disk, so a newly released Opus shows up with no code change;
                     0 to use only the built-in baseline)
  UC_STOCK_CACHE     optional path for the learned-stock cache (default: a
                     per-user state dir -- %LOCALAPPDATA%\\UltraCode-Shim or
                     $XDG_STATE_HOME/ultracode-shim)
  UC_STOCK_MODELS    optional JSON/CSV overriding the stock list entirely (wins
                     over both learned + built-in), e.g.
                     '["claude-opus-4-8","claude-sonnet-4-6"]' or a JSON array of
                     {"id","display_name"} objects
  UC_CONFIG          path to config.json (default: config.json beside proxy.py,
                     falling back to config.example.json)
  UC_MODEL_MAP       optional JSON, e.g. {"claude-opus-4-8":"my-model"}
  UC_LOG             optional log file path (default stderr)
  UC_VERBOSE         default 0
  UC_BROWSER_UA      User-Agent for openai_compat upstreams (default: modern
                     Chrome UA). Fixes CF 403 "browser_signature_banned" on
                     providers like crof.ai. Override with env or per-route
                     "headers".

ROUTE SHAPE (config.json "routes" object)
-----------------------------------------
  {
    "claude-opus-4-8":   {"model": "claude-opus-4-8",
                          "upstream": "https://api.anthropic.com",
                          "auth": "passthrough"},
    "claude-mimo":       {"type": "openai_compat",
                          "model": "mimo-v2.5-pro",
                          "upstream": "https://token-plan-sgp.xiaomimimo.com/v1",
                          "auth": "Bearer ${MIMO_API_KEY}"},
    "claude-gpt-5.5":    {"type": "codex_oauth", "model": "gpt-5.5"}
  }

  type     omit for Anthropic passthrough; "openai_compat"; "codex_oauth"; or
           "auto" (the Auto Router -- a cheap classifier model scores the other
           backends per task and routes to the cheapest one that clears a
           quality bar; see the "router" section in config.json and
           docs/AUTO_ROUTER.md)
  model    backend model id sent upstream
  upstream backend base URL. openai_compat: the OpenAI base URL from the
           provider's docs (usually ends in /v1); the proxy appends
           /chat/completions. passthrough: a base the inbound path is appended to.
  auth     "passthrough" (keep Claude Code's own credential) OR a literal
           header value: "Bearer ${KEY}" / "x-api-key: ${KEY}". ${VARS} are
           expanded from the environment (export them, or use a gitignored
           ultracode.env that the launchers load).
  headers  optional dict of extra request headers (values support ${VARS}).
  max_output_tokens  optional completion cap for openai_compat (default 8192).
  body     optional dict of extra params merged into the openai_compat request
           body (values support ${VARS}). e.g. MiniMax-M3 needs
           {"reasoning_split": true} so its <think> chain-of-thought is kept out
           of the visible answer.
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

LISTEN_HOST = os.environ.get("UC_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("UC_LISTEN_PORT", "8141"))
UPSTREAM = os.environ.get("UC_UPSTREAM", "https://api.anthropic.com").rstrip("/")
MAX_TOKENS_FLOOR = int(os.environ.get("UC_MAX_TOKENS", "64000"))
FORCE_EFFORT = os.environ.get("UC_FORCE_EFFORT", "xhigh")
FORCE_THINKING = os.environ.get("UC_FORCE_THINKING", "1") == "1"
INJECT_REMINDER = os.environ.get("UC_INJECT_REMINDER", "1") == "1"
INCLUDE_STOCK_MODELS = os.environ.get("UC_INCLUDE_STOCK_MODELS", "1") != "0"
LEARN_STOCK_MODELS = os.environ.get("UC_STOCK_LEARN", "1") != "0"
VERBOSE = os.environ.get("UC_VERBOSE", "0") == "1"
_LOG_PATH = os.environ.get("UC_LOG", "")

# --- Security & resource limits (issues #19, #22, #24) ------------------------
# Loopback Host-header guard: when bound to a loopback address (the default), the
# proxy serves only requests whose Host header is a loopback name. This blocks
# DNS-rebinding -- a malicious web page can't make a victim's browser read the
# backend config off /healthz or flip routing via /uc/select, because fetch()
# can't forge the Host header and the rebound hostname isn't loopback. Bind a
# non-loopback UC_LISTEN_HOST (LAN sharing) to opt out. (#19)
GUARD_LOCAL = os.environ.get("UC_GUARD_LOCAL", "1") != "0"
# Reject request bodies larger than this many bytes before allocating them
# (0 disables). Caps memory blowup from a single oversized upload. (#24)
MAX_BODY_BYTES = int(os.environ.get("UC_MAX_BODY_BYTES", str(64 * 1024 * 1024)))
# Cap concurrent in-flight requests so a connection flood can't spawn unbounded
# threads; excess connections wait for a slot (back-pressure). 0 disables. (#24)
MAX_CONNECTIONS = int(os.environ.get("UC_MAX_CONNECTIONS", "128"))
# Per-socket timeout (seconds) bounds idle / slowloris connections. Generous so
# it never trips an actively streaming response (each chunk resets it). (#24)
SOCKET_TIMEOUT = float(os.environ.get("UC_SOCKET_TIMEOUT", "660"))
# Inbound credentials never forwarded to a non-default, non-passthrough upstream
# (so a custom route can't exfiltrate Claude Code's own Anthropic key). (#22)
_INBOUND_CRED_HEADERS = ("authorization", "x-api-key")


def _is_loopback_listen() -> bool:
    h = LISTEN_HOST.strip().lower()
    return h in ("localhost", "127.0.0.1", "::1") or h.startswith("127.")


def _request_host_is_local(host_header) -> bool:
    """True if the inbound Host header names a loopback address. Handles
    host:port, bracketed IPv6 ([::1]:8141), and bare hostnames."""
    if not host_header:
        return False
    h = host_header.strip()
    if h.startswith("["):                       # [::1] or [::1]:port
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:                      # host:port (not bare IPv6)
        h = h.rsplit(":", 1)[0]
    h = h.strip().lower()
    return h in ("localhost", "127.0.0.1", "::1") or h.startswith("127.")

# Auto Router knobs (see the "router" section in config.json + docs/AUTO_ROUTER.md).
ROUTER_ENABLED_ENV = os.environ.get("UC_ROUTER", "1") != "0"
ROUTER_TIMEOUT = float(os.environ.get("UC_ROUTER_TIMEOUT", "12"))
ROUTER_MAX_TOKENS = int(os.environ.get("UC_ROUTER_MAX_TOKENS", "600"))
ROUTER_LOG = os.environ.get("UC_ROUTER_LOG", "0") == "1"

# Routing directives ("pins"): a prompt tag like [[route:codex]] / @codex forces a
# single request onto a specific backend, overriding orchestrator/worker selection
# AND the Auto Router. This is what lets an automated multi-agent workflow land each
# spawned sub-agent on the right model by role (plan->opus, code->composer, ...).
# OPT-IN: OFF unless turned on via "directives": {"enabled": true} in config.json
# (or UC_DIRECTIVES=1). Default => exact prior behavior, so this never disrupts an
# existing setup that hasn't asked for it. Final value is resolved in
# _configure_directives(); this is only the pre-config default. See docs/DIRECTIVES.md.
DIRECTIVES_ENABLED = os.environ.get("UC_DIRECTIVES") == "1"
DIRECTIVES_NL = os.environ.get("UC_DIRECTIVES_NL", "0") == "1"   # natural-language tier: opt-in (off by default)
DIRECTIVES_LOG = os.environ.get("UC_DIRECTIVES_LOG", "0") == "1"
DIRECTIVES = {"planner": None, "strip": True}   # filled from config in main()
_ROUTE_ALIASES = {}                              # normalized token -> concrete route id

# BROWSER_UA: browser UA for openai_compat (and classifier) calls.
# CF-protected providers (e.g. crof.ai) ban Python-urllib (error 1010
# "browser_signature_banned"). Matches droid/factory clients.
# Override: UC_BROWSER_UA=... or route "headers".
BROWSER_UA = os.environ.get(
    "UC_BROWSER_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

# 1M context window: Claude Code sizes its context meter (and auto-compaction) to
# 1M only when the model id it holds carries a "[1m]" suffix. For a real-Claude
# passthrough route whose upstream model is 1M-capable, we ADVERTISE the picker id
# with that suffix on /v1/models + /healthz, so even an in-session /model switch
# (not just a launch-time pick) gets the 1M window. The suffix is a client-side
# convention, not an Anthropic model id: it is stripped before routing and
# normalized off the sticky orchestrator/worker selection, so internal ids stay
# clean. Disable with UC_ADVERTISE_1M=0. See docs/DIRECTIVES.md / PR #8 + #10.
_ONEM_SUFFIX = "[1m]"
ADVERTISE_1M = os.environ.get("UC_ADVERTISE_1M", "1") != "0"
_CONTEXT_1M_UPSTREAM = set(t.strip() for t in os.environ.get(
    "UC_1M_UPSTREAM",
    "claude-opus-4-8,claude-opus-4-7,claude-opus-4-6,claude-sonnet-4-6").split(",") if t.strip())


def _strip_1m(mid):
    """Model id without a trailing [1m] window suffix (the client convention)."""
    if isinstance(mid, str) and mid.endswith(_ONEM_SUFFIX):
        return mid[:-len(_ONEM_SUFFIX)]
    return mid


def _advertise_id(model_entry):
    """The id to advertise for a configured model on /v1/models + /healthz. Appends
    [1m] when ADVERTISE_1M is on and the model is a real-Claude PASSTHROUGH route to
    a 1M-capable upstream model, so Claude Code renders the 1M window for it (incl.
    in-session /model picks). Worker entries and non-passthrough routes are returned
    unchanged. Never raises."""
    mid = model_entry.get("id") if isinstance(model_entry, dict) else None
    if not (ADVERTISE_1M and isinstance(mid, str)):
        return mid
    if mid.endswith(_ONEM_SUFFIX) or mid.startswith(WORKER_ID_PREFIX):
        return mid
    slot = UC_SLOT_MAP.get(mid)
    if not isinstance(slot, dict) or slot.get("type") not in (None, "anthropic"):
        return mid                                  # passthrough (real Claude) only
    if (slot.get("model") or mid) in _CONTEXT_1M_UPSTREAM:
        return mid + _ONEM_SUFFIX
    return mid


def _display_name_for_id(mid):
    if not mid:
        return None
    for m in UC_MODELS:
        if m.get("id") == mid:
            return m.get("display_name", mid)
    for m in _stock_models():
        if m.get("id") == mid:
            return m.get("display_name", mid)
    return mid


def _orchestrator_worker_status():
    with _SEL_LOCK:
        active = dict(_ACTIVE)
    orch = active.get("orch")
    worker = active.get("worker")
    return {
        "enabled": ORCH_WORKER,
        "orchestrator": {"id": orch, "display_name": _display_name_for_id(orch)},
        "worker": {"id": worker, "display_name": _display_name_for_id(worker)},
        "worker_explicit": active.get("worker_explicit", False),
        "same_model": bool(orch and worker and orch == worker),
    }


def _context_length_hint(detail):
    low = (detail or "").lower()
    if any(x in low for x in ("context", "token", "maximum context",
                              "too long", "too many tokens", "length exceeded")):
        return (" (This backend rejected the full conversation history — the proxy "
                "forwards the entire transcript with no trimming. Try compacting the "
                "session, switching to a backend with a larger context window, or "
                "starting a fresh session.)")
    return ""


try:
    UC_MODEL_MAP = json.loads(os.environ.get("UC_MODEL_MAP", "") or "{}")
    if not isinstance(UC_MODEL_MAP, dict):
        UC_MODEL_MAP = {}
except Exception:
    UC_MODEL_MAP = {}

# Optional Codex/ChatGPT OAuth helper (only needed for "codex_oauth" routes).
try:
    from providers import codex_oauth as _codex_oauth  # type: ignore
except Exception:
    try:
        import codex_oauth as _codex_oauth  # type: ignore
    except Exception:
        _codex_oauth = None

# Optional Cursor Composer helper (only needed for "cursor_agent" routes).
try:
    from providers import cursor_agent as _cursor_agent  # type: ignore
except Exception:
    try:
        import cursor_agent as _cursor_agent  # type: ignore
    except Exception:
        _cursor_agent = None


_ENV_TOKEN = "${"


def _expand_env(value):
    """Expand ${VAR} references in a string from os.environ. Unknown vars
    expand to empty string. Non-strings pass through unchanged."""
    if not isinstance(value, str) or _ENV_TOKEN not in value:
        return value
    out = []
    i = 0
    n = len(value)
    while i < n:
        if value[i] == "$" and i + 1 < n and value[i + 1] == "{":
            end = value.find("}", i + 2)
            if end != -1:
                var = value[i + 2:end]
                out.append(os.environ.get(var, ""))
                i = end + 1
                continue
        out.append(value[i])
        i += 1
    return "".join(out)


def _default_config_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("config.json", "config.example.json"):
        p = os.path.join(here, name)
        if os.path.isfile(p):
            return p
    return os.path.join(here, "config.json")


def _strip_comments(obj):
    """Drop keys that start with '_' (used for inline documentation)."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(x) for x in obj]
    return obj


def load_config(path):
    """Load the single config.json (proxy/models/routes), stripping comments."""
    with open(path, "r", encoding="utf-8") as f:
        return _strip_comments(json.load(f))


def _routes_to_slots(routes):
    """routes{} from config.json -> UC_SLOT_MAP. Expands ${ENV} in model/upstream/auth/headers."""
    out = {}
    if not isinstance(routes, dict):
        return out
    for mid, route in routes.items():
        if not isinstance(route, dict):
            continue
        slot = {}
        if route.get("model"):
            slot["model"] = _expand_env(route["model"])
        if route.get("upstream"):
            slot["upstream"] = _expand_env(route["upstream"]).rstrip("/")
        auth = route.get("auth")
        if auth == "passthrough":
            # Keep the passthrough intent on the slot so dispatch knows to forward
            # Claude Code's own credential to this upstream (vs. stripping it). (#22)
            slot["auth_passthrough"] = True
        elif auth:
            slot["auth"] = _expand_env(auth)
        if route.get("type"):
            slot["type"] = route["type"]
        if route.get("max_output_tokens"):
            slot["max_output_tokens"] = route["max_output_tokens"]
        if isinstance(route.get("headers"), dict):
            slot["headers"] = {k: _expand_env(v) for k, v in route["headers"].items()}
        if isinstance(route.get("body"), dict):
            slot["body"] = route["body"]  # carried raw; ${ENV} expanded at use-site
        out[mid] = slot
    return out


def _models_from_config(models):
    out = []
    for m in models or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid or not isinstance(mid, str):
            continue
        out.append({
            "type": "model",
            "id": mid,
            "display_name": m.get("display_name") or mid,
            "created_at": m.get("created_at") or "2025-01-01T00:00:00Z",
        })
    return out


# Stock (real Claude) models. These are advertised on /v1/models in addition to
# whatever Anthropic's own /v1/models returns, so real Claude never disappears
# from the /model picker -- e.g. when there's no Anthropic credential to forward
# upstream, or the upstream fetch hiccups. They are NOT orchestrator/worker
# picker entries: stock ids must keep flowing through _select_target untouched
# so the dynamic-workflow background traffic (hardcoded to claude-opus-4-8) can
# still be remapped onto your pick instead of hijacking the selection.
#
# This is the built-in *baseline* (a floor, current at release time). At runtime
# the proxy also LEARNS the real Claude ids from any successful upstream
# /v1/models fetch and caches them to disk (see _learn_stock_from_upstream /
# UC_STOCK_LEARN), so a newly released Opus appears automatically with no code
# change. Precedence when building the advertised list: UC_STOCK_MODELS override
# (if set) wins outright; otherwise learned-from-upstream entries win over the
# baseline, and the baseline fills in anything not yet learned. Disable the whole
# thing with UC_INCLUDE_STOCK_MODELS=0; disable just learning with UC_STOCK_LEARN=0.
STOCK_MODELS = [
    {"id": "claude-opus-4-8",   "display_name": "Claude Opus 4.8"},
    {"id": "claude-opus-4-7",   "display_name": "Claude Opus 4.7"},
    {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
    {"id": "claude-haiku-4-5",  "display_name": "Claude Haiku 4.5"},
]

# Which upstream ids count as "real Claude" worth learning. Anthropic's
# /v1/models returns ids like "claude-opus-4-8" / "claude-haiku-4-5-20251001";
# we keep the dated and dateless forms but skip anything that isn't a Claude id.
_STOCK_LEARN_RE = re.compile(r"^(claude|anthropic)[.-]", re.I)

# A trailing -YYYYMMDD / @YYYYMMDD snapshot suffix (pre-4.6 models ship dated;
# the dateless alias points at the same model). _model_family collapses the two
# so we never advertise both "claude-haiku-4-5" and "claude-haiku-4-5-20251001".
_DATE_SUFFIX_RE = re.compile(r"[-@]\d{8}$")


def _model_family(mid):
    """Key that treats a model's dated and dateless ids as the same thing, so the
    stock list doesn't show near-duplicate rows for one model."""
    return _DATE_SUFFIX_RE.sub("", mid or "").lower()

# Learned stock cache (populated from disk at startup + refreshed on every
# successful upstream /v1/models fetch). Guarded by a lock for the threaded server.
_LEARNED_STOCK = []          # [{"id","display_name"}], most-recent upstream order
_LEARNED_STOCK_LOCK = threading.Lock()
_LEARNED_STOCK_LOADED = False


def _stock_cache_path():
    """Where the learned-stock cache lives. UC_STOCK_CACHE overrides; otherwise a
    per-user state dir that matches the launchers' conventions."""
    p = os.environ.get("UC_STOCK_CACHE")
    if p:
        return p
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "UltraCode-Shim", "stock-models.json")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "ultracode-shim", "stock-models.json")


def _normalize_learned(items):
    """Coerce a list of {"id","display_name"} into the normalized, Claude-only
    form, deduped by id (first occurrence wins)."""
    out, seen = [], set()
    for m in items or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or mid in seen or not _STOCK_LEARN_RE.match(mid):
            continue
        seen.add(mid)
        out.append({"id": mid, "display_name": m.get("display_name") or mid})
    return out


def _load_learned_stock():
    """Load the learned-stock cache from disk into _LEARNED_STOCK (once)."""
    global _LEARNED_STOCK, _LEARNED_STOCK_LOADED
    if _LEARNED_STOCK_LOADED or not LEARN_STOCK_MODELS:
        return
    _LEARNED_STOCK_LOADED = True
    path = _stock_cache_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        models = data.get("models") if isinstance(data, dict) else data
        learned = _normalize_learned(models)
        if learned:
            with _LEARNED_STOCK_LOCK:
                _LEARNED_STOCK = learned
            vlog("loaded %d learned stock model(s) from %s" % (len(learned), path))
    except FileNotFoundError:
        pass
    except Exception as e:
        vlog("could not read learned-stock cache %s: %s" % (path, e))


def _learn_stock_from_upstream(upstream_data):
    """Given the 'data' list from a successful upstream /v1/models response, learn
    the real Claude ids: update the in-memory cache and persist to disk if it
    changed. Best-effort; never raises into the request path."""
    global _LEARNED_STOCK
    if not LEARN_STOCK_MODELS:
        return
    learned = _normalize_learned(upstream_data)
    if not learned:
        return
    with _LEARNED_STOCK_LOCK:
        changed = [m["id"] for m in learned] != [m["id"] for m in _LEARNED_STOCK]
        _LEARNED_STOCK = learned
    if not changed:
        return
    path = _stock_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": int(time.time()), "upstream": UPSTREAM,
                       "models": learned}, f)
        os.replace(tmp, path)
        vlog("learned %d stock Claude model(s) from upstream -> %s"
             % (len(learned), path))
    except Exception as e:
        vlog("could not write learned-stock cache %s: %s" % (path, e))


def _parse_stock_override(raw):
    """UC_STOCK_MODELS may be a JSON array of ids, a JSON array of
    {"id","display_name"} objects, or a comma-separated list of ids. Returns a
    normalized [{"id","display_name"}] list, or None if the var is unset/empty
    (use the built-in default) -- an explicit empty list disables stock models."""
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = None
    if raw[0] in "[{":
        try:
            parsed = json.loads(raw)
        except Exception as e:
            log("UC_STOCK_MODELS is not valid JSON (%s); using the built-in stock list" % e)
            return None
    if parsed is None:  # CSV form: "claude-opus-4-8, claude-sonnet-4-6"
        parsed = [s.strip() for s in raw.split(",")]
    out = []
    for item in parsed if isinstance(parsed, list) else []:
        if isinstance(item, str) and item.strip():
            mid = item.strip()
            out.append({"id": mid, "display_name": mid})
        elif isinstance(item, dict) and item.get("id"):
            out.append({"id": item["id"],
                        "display_name": item.get("display_name") or item["id"]})
    return out


def _stock_source():
    """The stock model list to advertise, as [{"id","display_name"}], BEFORE the
    discovery id-rule filter. Precedence:
      1. UC_STOCK_MODELS override -> exactly that list (learning ignored).
      2. otherwise: learned-from-upstream ids (current real Claude) first, then
         the built-in baseline fills in anything not learned yet.
    So a freshly released Opus shows up the moment upstream lists it, while the
    baseline still guarantees real Claude even before anything is learned."""
    override = _parse_stock_override(os.environ.get("UC_STOCK_MODELS"))
    if override is not None:
        return override
    _load_learned_stock()
    with _LEARNED_STOCK_LOCK:
        learned = list(_LEARNED_STOCK)
    # Learned (real upstream ids) first, then the baseline fills in the rest.
    # Dedup by model *family* so a model's dated and dateless ids (e.g.
    # claude-haiku-4-5-20251001 vs claude-haiku-4-5) collapse to one row -- the
    # learned upstream id wins since that's what Anthropic actually serves.
    out, seen_id, seen_family = [], set(), set()
    for m in learned + STOCK_MODELS:
        mid = m.get("id")
        if not mid or mid in seen_id:
            continue
        fam = _model_family(mid)
        if fam in seen_family:
            continue
        seen_id.add(mid)
        seen_family.add(fam)
        out.append(m)
    return out


def _stock_models():
    """The stock Claude models to advertise, after applying the stock source
    precedence (override / learned / baseline) and the /^(claude|anthropic)/i id
    rule Claude Code enforces on discovery."""
    if not INCLUDE_STOCK_MODELS:
        return []
    out = []
    for m in _stock_source():
        mid = m.get("id")
        if not mid or not re.match(r"^(claude|anthropic)", mid, re.I):
            continue
        out.append({
            "type": "model",
            "id": mid,
            "display_name": m.get("display_name") or mid,
            "created_at": "2025-01-01T00:00:00Z",
        })
    return out


ULTRACODE_REMINDER = (
    "Ultracode is on: optimize for the most exhaustive, correct answer \u2014 not "
    "the fastest or cheapest. Use the Workflow tool on every substantive task; "
    "token cost is not a constraint. See the Workflow tool's **Ultracode** "
    "section and quality patterns. Solo only on conversational/trivial turns."
)
_REMINDER_FINGERPRINT = "Ultracode is on:"

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    with _log_lock:
        if _LOG_PATH:
            try:
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                return
            except Exception:
                pass
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


def vlog(msg: str) -> None:
    if VERBOSE:
        log(msg)


DEFAULT_UPSTREAM = UPSTREAM
UC_SLOT_MAP = {}   # populated in main()
UC_MODELS = []     # populated in main()

# Auto Router state (populated in main() from config.json "router").
ROUTER = {
    "enabled": False,
    "id": "claude-auto",      # the picker id that triggers smart routing
    "classifier": None,       # route id of the cheap model that scores candidates
    "threshold": 0.7,         # success-probability bar; cheapest candidate >= this wins
    "candidates": [],         # [{"id","cost","card","supports_images"}]
    "default": None,          # fallback candidate id when classification can't run
    "cache": True,            # reuse the decision across a task's tool-call round-trips
}


# --------------------------------------------------------------------------
# Orchestrator + Worker (two-model dynamic workflows)
# --------------------------------------------------------------------------
# Claude Code's /model picker is single-slot, and its dynamic-workflow machinery
# issues most of its background traffic as the stock model (claude-opus-4-8 etc.)
# regardless of your pick -- so the sub-agents/leaves that do the bulk of a
# workflow's work don't follow your selection. This proxy fixes that by holding a
# sticky two-tier selection and routing EVERY request by tier:
#   heavy (orchestrator: the main interactive loop -- carries an interactive-only
#          tool like AskUserQuestion/ExitPlanMode) -> the orchestrator model
#   fast  (worker: every Workflow/Task sub-agent + background call)
#          -> the worker model
# main() auto-adds a "Worker -> X" picker entry (id claude-worker-X) for each of
# your models, so you can pick an orchestrator AND a worker from /model. A plain
# pick sets BOTH tiers (one model everywhere); a "Worker -> X" pick sets only the
# worker tier. Stock opus/sonnet/haiku ids never change the selection -- they are
# remapped to it, so background workflow traffic follows your pick instead of
# silently billing the stock model. Disable with UC_ORCH_WORKER=0.
ORCH_WORKER = os.environ.get("UC_ORCH_WORKER", "1") == "1"
WORKER_ID_PREFIX = "claude-worker-"
TIER_LOG = os.environ.get("UC_TIER_LOG", "0") == "1"
# Tools the harness hands ONLY to the main interactive loop (never to Workflow/
# Task sub-agents). Their presence marks the orchestrator ("heavy") -- a far more
# reliable structural signal than scraping the system prompt.
_INTERACTIVE_ONLY_TOOLS = frozenset({
    "AskUserQuestion", "ExitPlanMode", "EnterPlanMode",
})
_SEL_LOCK = threading.Lock()
_ACTIVE = {"orch": None, "worker": None, "worker_explicit": False}
_ORCH_PICK_IDS = set()   # base orchestrator picker ids (filled in main())
_WORKER_MAP = {}         # claude-worker-<x> -> claude-<x>  (filled in main())
_WARNED_NO_SELECTION = False


def _selection_cache_path() -> str:
    """Where the sticky orchestrator/worker selection is persisted, or "" to
    disable. Set by the launchers (UC_SELECTION_CACHE) so the pick survives a
    proxy restart -- otherwise a restarted proxy forgets the selection and
    workflow sub-agents silently fall back to stock Claude. (issue #18)"""
    return os.environ.get("UC_SELECTION_CACHE", "").strip()


def _save_selection() -> None:
    path = _selection_cache_path()
    if not path:
        return
    try:
        with _SEL_LOCK:
            data = {"orch": _ACTIVE["orch"], "worker": _ACTIVE["worker"],
                    "worker_explicit": _ACTIVE["worker_explicit"]}
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        vlog("selection persist failed: %s" % e)


def _load_selection() -> None:
    path = _selection_cache_path()
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        vlog("selection restore failed: %s" % e)
        return
    if not isinstance(data, dict):
        return
    with _SEL_LOCK:
        _ACTIVE["orch"] = data.get("orch") or None
        _ACTIVE["worker"] = data.get("worker") or None
        _ACTIVE["worker_explicit"] = bool(data.get("worker_explicit"))
    if _ACTIVE["orch"] or _ACTIVE["worker"]:
        log("restored orchestrator/worker selection: orch=%s worker=%s"
            % (_ACTIVE["orch"], _ACTIVE["worker"]))


def _has_active_selection() -> bool:
    with _SEL_LOCK:
        return bool(_ACTIVE["orch"] or _ACTIVE["worker"])


def _warn_no_selection_once(model) -> None:
    """Explain (once) why traffic is using stock Claude: orchestrator/worker is on
    but nothing is selected yet, so a stock/unknown id passes through unchanged.
    This is the most common cause of "workflows ignore my models". (issue #18)"""
    global _WARNED_NO_SELECTION
    if _WARNED_NO_SELECTION:
        return
    _WARNED_NO_SELECTION = True
    log("orchestrator/worker is ON but no model is selected yet; request for '%s' "
        "is passing through as stock Claude. Pick a model in the pre-launch "
        "selector or via /model so workflow sub-agents use it. (issue #18)" % model)


def _request_tier(body: dict) -> str:
    """"heavy" for the main interactive loop (carries an interactive-only tool),
    "fast" for every Workflow/Task sub-agent + background call."""
    if not ORCH_WORKER:
        return "heavy"
    tools = body.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict) and t.get("name") in _INTERACTIVE_ONLY_TOOLS:
                return "heavy"
    return "fast"


def _set_selection(orch=None, worker=None):
    """Directly pre-set the sticky orchestrator/worker selection (used by the
    two-column pre-launch selector via POST /uc/select). Either may be None to
    leave that tier unchanged. Returns the resolved active selection dict."""
    orch, worker = _strip_1m(orch), _strip_1m(worker)   # selections store clean ids
    with _SEL_LOCK:
        if orch is not None:
            _ACTIVE["orch"] = orch or None
        if worker is not None:
            _ACTIVE["worker"] = worker or None
            _ACTIVE["worker_explicit"] = bool(worker)
        if orch and worker is None and not _ACTIVE["worker_explicit"]:
            _ACTIVE["worker"] = orch
        active = dict(_ACTIVE)
    _save_selection()
    return active


def _select_target(mid, tier: str):
    """Update the sticky orchestrator/worker selection from a deliberate pick,
    then return the picker id this request should route to (by tier). Returns
    ``mid`` unchanged when the feature is off or no selection is active yet, so
    fresh sessions behave exactly as before."""
    if not ORCH_WORKER:
        return mid
    mid = _strip_1m(mid)   # a [1m]-suffixed pick maps to its clean route id
    picked = False
    with _SEL_LOCK:
        if mid in _WORKER_MAP:
            _ACTIVE["worker"] = _WORKER_MAP[mid]
            _ACTIVE["worker_explicit"] = True
            picked = True
        elif mid in _ORCH_PICK_IDS:
            _ACTIVE["orch"] = mid
            if not _ACTIVE["worker_explicit"]:
                _ACTIVE["worker"] = mid
            picked = True
        # else: stock (opus/sonnet/haiku) or unknown id -> not a selection.
        orch = _ACTIVE["orch"]
        worker = _ACTIVE["worker"]
    if picked:
        _save_selection()   # persist deliberate /model picks across restarts (#18)
    target = (orch or worker) if tier == "heavy" else (worker or orch)
    return target or mid


def _wire_orchestrator_worker():
    """Populate the orchestrator-pick ids + worker map from UC_MODELS, and append
    a synthesized "Worker -> X" picker entry (routed like its base model) for each
    advertised model. Idempotent; called from main() after models/slots load."""
    if not (ORCH_WORKER and UC_MODELS):
        return
    for m in list(UC_MODELS):
        mid = m.get("id")
        if not mid or mid in _WORKER_MAP or mid.startswith(WORKER_ID_PREFIX):
            continue
        _ORCH_PICK_IDS.add(mid)
        suffix = mid[len("claude-"):] if mid.startswith("claude-") else mid
        wid = WORKER_ID_PREFIX + suffix
        if wid in _WORKER_MAP:
            continue
        _WORKER_MAP[wid] = mid
        UC_MODELS.append({
            "type": "model", "id": wid,
            "display_name": "Worker \u2192 %s" % m.get("display_name", mid),
            "created_at": m.get("created_at") or "2025-01-01T00:00:00Z",
        })
        if mid in UC_SLOT_MAP and wid not in UC_SLOT_MAP:
            UC_SLOT_MAP[wid] = dict(UC_SLOT_MAP[mid])
    if _WORKER_MAP:
        log("orchestrator+worker enabled: %d model(s), worker ids: %s"
            % (len(_ORCH_PICK_IDS), ", ".join(sorted(_WORKER_MAP))))


# --------------------------------------------------------------------------
# Routing directives ("pins") -- force a request onto a specific backend
# --------------------------------------------------------------------------
# A workflow (or a human) can tag a request's prompt to pin it to ONE backend,
# overriding the orchestrator/worker selection AND the Auto Router. This is how an
# automated multi-agent workflow lands each spawned sub-agent on the right model by
# role -- e.g. plan->opus, code->composer, review->codex, fix->claude -- with no
# turn-by-turn driving: the workflow script bakes a role tag into each agent()
# prompt and the proxy hard-pins that request.
#
# Marker tiers (case-insensitive), most explicit first; a tier wins only if it
# resolves to EXACTLY ONE configured backend (naming two models is ambiguous ->
# ignored, normal routing decides):
#   1. [[route:codex]]                         sentinel  (stripped before forwarding)
#   2. @codex  use:codex  route:codex  model:codex   tag   (stripped)
#   3. "...have codex review...", "ask codex to ..."  natural language (UC_DIRECTIVES_NL)
#
# The token after a marker is resolved through an alias table auto-derived from
# your model ids + display names (plus router.aliases / directives.aliases
# overrides). A pin to an unconfigured or "auto" route is ignored so a request is
# never broken.
_DIRECTIVE_SENTINEL = re.compile(r"\[\[\s*(?:route|model|use)\s*:\s*([A-Za-z0-9._\-]+)\s*\]\]", re.I)
_DIRECTIVE_TAG = re.compile(r"(?<![^\s(])(?:@|(?:route|model|use)\s*:\s*)([A-Za-z0-9._\-]+)", re.I)
_DIRECTIVE_NL = re.compile(r"\b(?:use|using|have|ask|let|route\s+to|via|with)\s+([A-Za-z0-9._\-]+)", re.I)


def _norm_alias(s):
    """Lowercase + strip non-alphanumerics so 'GPT-5.5', 'gpt5.5', 'gpt_5_5' all
    collapse to one matchable key."""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def _resolve_alias(token):
    return _ROUTE_ALIASES.get(_norm_alias(token))


def _latest_user_turn(anth_body):
    """(message_dict, plain_text) of the newest user turn carrying real
    instruction text. Pure tool_result turns (tool round-trips) are skipped so a
    sub-agent's task tag stays sticky across its tool calls. (None, "") if none."""
    for m in reversed(anth_body.get("messages") or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            non_tool = [b for b in content
                        if not (isinstance(b, dict) and b.get("type") == "tool_result")]
            if not non_tool:
                continue
            txt = _text_from_anthropic_content(non_tool)
        else:
            txt = content if isinstance(content, str) else _text_from_anthropic_content(content)
        txt = (txt or "").strip()
        if txt:
            return m, txt
    return None, ""


def _detect_directive(text):
    """(route_ids, spans, tier) for the most explicit marker tier that resolves to
    one or more configured backends. `spans` are the literal marker substrings to
    strip (empty for the natural-language tier -- that's prose, left intact)."""
    def scan(pattern):
        ids, spans, seen = [], [], set()
        for m in pattern.finditer(text):
            rid = _resolve_alias(m.group(1))
            if not rid:
                continue
            spans.append(m.group(0))
            if rid not in seen:
                seen.add(rid)
                ids.append(rid)
        return ids, spans
    ids, spans = scan(_DIRECTIVE_SENTINEL)
    if ids:
        return ids, spans, "sentinel"
    ids, spans = scan(_DIRECTIVE_TAG)
    if ids:
        return ids, spans, "tag"
    if DIRECTIVES_NL:
        ids, _ = scan(_DIRECTIVE_NL)
        if ids:
            return ids, [], "nl"
    return [], [], None


def _strip_spans_in_msg(msg, spans):
    """Remove matched marker substrings from a user turn's text in-place so the
    backend model never sees the routing tag."""
    if not spans or not isinstance(msg, dict):
        return
    def clean(s):
        # Remove the marker itself; do NOT globally collapse whitespace -- that
        # would flatten indentation in any code the prompt carries. Only tidy
        # trailing spaces left on a line and trim the ends.
        for sp in spans:
            s = s.replace(sp, "")
        return re.sub(r"[ \t]+(\n|$)", r"\1", s).strip()
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = clean(content)
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                b["text"] = clean(b["text"])


def _directive_pin(body):
    """Route id this request is pinned to by a prompt directive, or None. Strips
    the marker text in-place when a pin is found. Never raises."""
    if not DIRECTIVES_ENABLED:
        return None
    try:
        msg, text = _latest_user_turn(body)
        if not text:
            return None
        ids, spans, tier = _detect_directive(text)
        if len(ids) != 1:
            if len(ids) > 1 and DIRECTIVES_LOG:
                log("[directive] ambiguous (%s named); ignored" % ", ".join(ids))
            return None
        rid = ids[0]
        slot = UC_SLOT_MAP.get(rid)
        if not isinstance(slot, dict) or slot.get("type") == "auto":
            if DIRECTIVES_LOG:
                log("[directive] '%s' (%s) not a usable backend; ignored" % (rid, tier))
            return None
        if DIRECTIVES.get("strip", True) and spans:
            _strip_spans_in_msg(msg, spans)
        return rid
    except Exception as e:
        if DIRECTIVES_LOG:
            log("[directive] error: %s" % e)
        return None


def _is_plan_mode(body):
    """True when the request is the interactive planning loop (the harness offers
    ExitPlanMode only while in plan mode)."""
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("name") == "ExitPlanMode":
            return True
    return False


def _configure_directives(cfg):
    """Build the alias table for prompt routing directives from configured
    models/routes, plus optional overrides. Idempotent; called from main()."""
    global _ROUTE_ALIASES, DIRECTIVES_ENABLED
    if not isinstance(cfg, dict):
        cfg = {}
    aliases = {}
    STOP = {"the", "real", "auto", "smart", "routing", "router", "worker", "experimental",
            "cursor", "oauth", "fast", "flash", "pro", "plus", "max", "mini", "via", "pay",
            "you", "model", "plan", "code", "chat", "api", "beta", "preview"}

    def add(token, rid):
        key = _norm_alias(token)
        if not key or key in STOP:
            return
        if key in aliases:
            if aliases[key] != rid:
                aliases[key] = None          # collision -> ambiguous, disable
        else:
            aliases[key] = rid

    display = {m.get("id"): m.get("display_name", "") for m in (UC_MODELS or [])}
    for rid, slot in (UC_SLOT_MAP or {}).items():
        if not isinstance(slot, dict) or slot.get("type") == "auto":
            continue
        if rid.startswith(WORKER_ID_PREFIX):
            continue
        add(rid, rid)
        if rid.startswith("claude-"):
            add(rid[len("claude-"):], rid)
        for w in re.findall(r"[A-Za-z][A-Za-z0-9.]+", display.get(rid, "")):
            if w.lower() not in STOP and len(w) >= 3:
                add(w.lower(), rid)
        mv = slot.get("model")
        if isinstance(mv, str) and mv:
            seg = mv.split("/")[-1]
            head = re.split(r"[^A-Za-z]", seg)[0]
            if head and head.lower() not in STOP and len(head) >= 3:
                add(head.lower(), rid)
    aliases = {k: v for k, v in aliases.items() if v}   # drop ambiguous

    # Explicit overrides always win (directives.aliases preferred over router.aliases).
    rcfg = cfg.get("router") if isinstance(cfg.get("router"), dict) else {}
    dcfg = cfg.get("directives") if isinstance(cfg.get("directives"), dict) else {}
    for src in (rcfg.get("aliases"), dcfg.get("aliases")):
        if isinstance(src, dict):
            for tok, rid in src.items():
                if isinstance(rid, str) and rid in UC_SLOT_MAP:
                    aliases[_norm_alias(tok)] = rid
    _ROUTE_ALIASES = aliases

    planner = dcfg.get("planner") or rcfg.get("planner")
    DIRECTIVES["planner"] = planner if planner in UC_SLOT_MAP else None
    DIRECTIVES["strip"] = bool(dcfg.get("strip", True))
    # Opt-in resolution: an explicit env var wins (UC_DIRECTIVES=1 on, =0 off);
    # otherwise follow config, which defaults to OFF so a fresh upgrade is a no-op.
    env = os.environ.get("UC_DIRECTIVES")
    if env is not None:
        DIRECTIVES_ENABLED = env != "0"
    else:
        DIRECTIVES_ENABLED = bool(dcfg.get("enabled", False))
    if DIRECTIVES_ENABLED and aliases:
        log("directives: %d alias(es) over %s%s"
            % (len(aliases), ", ".join(sorted(set(aliases.values()))),
               ("; planner=%s" % DIRECTIVES["planner"]) if DIRECTIVES["planner"] else ""))


# --------------------------------------------------------------------------
# UltraCode envelope (the heart of the proxy)
# --------------------------------------------------------------------------

def _system_has_reminder(system) -> bool:
    if system is None:
        return False
    if isinstance(system, str):
        return _REMINDER_FINGERPRINT in system
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                txt = block.get("text", "")
                if isinstance(txt, str) and _REMINDER_FINGERPRINT in txt:
                    return True
            elif isinstance(block, str) and _REMINDER_FINGERPRINT in block:
                return True
    return False


def _inject_reminder(body: dict) -> None:
    if _system_has_reminder(body.get("system")):
        return
    block = {"type": "text", "text": ULTRACODE_REMINDER}
    system = body.get("system")
    if system is None:
        body["system"] = [block]
    elif isinstance(system, str):
        body["system"] = system.rstrip() + "\n\n" + ULTRACODE_REMINDER
    elif isinstance(system, list):
        system.append(block)
    else:
        body["system"] = [{"type": "text", "text": str(system)}, block]


def transform_messages_body(raw: bytes):
    """Apply the ultracode envelope and resolve the routing slot.

    Returns (body_bytes, route). On parse failure returns the original bytes
    with an empty route so the proxy never breaks a request.
    """
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as e:
        vlog("body parse failed, passing through: %s" % e)
        return raw, {}
    if not isinstance(body, dict):
        return raw, {}

    changed = False
    model_before = body.get("model")
    route = {}

    # 1M context window: Claude Code appends a "[1m]" suffix to a model id to ask
    # the client for the 1M window (it also sends the context-1m beta header). That
    # suffix is a client-side convention, not an Anthropic model id, so it must not
    # reach routing (it wouldn't match a route) or the upstream. Strip it up front
    # so "<id>[1m]" behaves exactly like "<id>" everywhere below; the 1M window is
    # unaffected because it comes from the beta header, which we leave untouched.
    stripped = _strip_1m(model_before)
    if stripped != model_before:
        model_before = stripped
        body["model"] = model_before
        changed = True

    # Orchestrator/Worker: classify tier and remap the model id to the selected
    # orchestrator (heavy) or worker (fast) model. This also captures the dynamic
    # workflow's stock-model background traffic so it follows your pick.
    tier = _request_tier(body)
    routed_id = _select_target(model_before, tier)
    if routed_id != model_before:
        body["model"] = routed_id
        changed = True
    elif (ORCH_WORKER and not _has_active_selection()
          and routed_id not in UC_SLOT_MAP):
        # Selection is on but empty AND this id has no route -> it will pass
        # through as stock Claude. Surface why (once) so a lost/never-made pick
        # doesn't look like the workflow silently ignoring the chosen model. (#18)
        _warn_no_selection_once(routed_id)
    if TIER_LOG:
        remap = ("%s->%s" % (model_before, routed_id)) if routed_id != model_before else (model_before or "-")
        log("tier=%s model=%s" % (tier, remap))

    # Routing directive ("pin"): a prompt tag forces THIS request onto a specific
    # backend, overriding the worker/orchestrator selection above AND the Auto
    # Router below (the pin sets a concrete model id, so the type=="auto" branch
    # never fires). This is how an automated multi-agent workflow lands each
    # spawned sub-agent on the right model by role. Falls back silently to normal
    # routing when no (or an ambiguous/unknown) directive is present.
    pin_id = _directive_pin(body)
    if pin_id and pin_id != body.get("model"):
        if DIRECTIVES_LOG or TIER_LOG or ROUTER_LOG:
            log("directive pin: tier=%s %s -> %s" % (tier, body.get("model"), pin_id))
        body["model"] = pin_id
        changed = True
    elif (DIRECTIVES_ENABLED and not pin_id and DIRECTIVES.get("planner")
          and _is_plan_mode(body) and DIRECTIVES["planner"] != body.get("model")):
        # No explicit pin, but this is the interactive planning loop -> planner.
        # Gated on DIRECTIVES_ENABLED so "enabled:false" / UC_DIRECTIVES=0 is a
        # true hard-off (the planner is otherwise applied independently of pins).
        planner = DIRECTIVES["planner"]
        if DIRECTIVES_LOG or TIER_LOG or ROUTER_LOG:
            log("directive plan-mode: tier=%s %s -> %s" % (tier, body.get("model"), planner))
        body["model"] = planner
        changed = True

    # Auto Router: a slot of type "auto" is not a real backend -- it asks a cheap
    # classifier model to score the configured candidates and routes this request
    # to the cheapest one that clears the quality bar. Resolve it to a concrete
    # candidate id, then fall through to that candidate's slot below.
    slot = UC_SLOT_MAP.get(body.get("model"))
    if isinstance(slot, dict) and slot.get("type") == "auto":
        picked = resolve_auto(body, tier) if _router_is_enabled() else None
        if not picked:
            # Router off or unresolvable -> deterministic fallback so we never try
            # to dispatch the synthetic "auto" id at a real backend.
            picked = _router_fallback_id(_router_available_candidates())
        if picked and picked != body.get("model"):
            if TIER_LOG or ROUTER_LOG:
                log("router tier=%s %s -> %s" % (tier, body.get("model"), picked))
            body["model"] = picked
            changed = True
        slot = UC_SLOT_MAP.get(body.get("model"))

    if isinstance(slot, dict) and slot.get("type") != "auto":
        target_model = slot.get("model")
        if target_model and target_model != body.get("model"):
            body["model"] = target_model
            changed = True
        up = slot.get("upstream")
        if up:
            route["upstream"] = up.rstrip("/")
        auth = slot.get("auth")
        if auth:
            route["auth"] = auth
        if slot.get("auth_passthrough"):
            # User explicitly opted to forward Claude Code's own credential to
            # this upstream; otherwise inbound creds are stripped at dispatch. (#22)
            route["auth_passthrough"] = True
        stype = slot.get("type")
        if stype:
            route["type"] = stype
        mot = slot.get("max_output_tokens")
        if mot:
            route["max_output_tokens"] = mot
        hdrs = slot.get("headers")
        if isinstance(hdrs, dict):
            route["headers"] = {k: _expand_env(v) for k, v in hdrs.items()}
        sbody = slot.get("body")
        if isinstance(sbody, dict):
            route["body"] = sbody
    elif model_before in UC_MODEL_MAP:
        body["model"] = UC_MODEL_MAP[model_before]
        changed = True

    if FORCE_EFFORT:
        oc = body.get("output_config")
        if not isinstance(oc, dict):
            oc = {}
        if oc.get("effort") != FORCE_EFFORT:
            oc["effort"] = FORCE_EFFORT
            body["output_config"] = oc
            changed = True
        elif "output_config" not in body:
            body["output_config"] = oc
            changed = True

    if FORCE_THINKING:
        th = body.get("thinking")
        if not isinstance(th, dict) or th.get("type") not in ("adaptive", "enabled"):
            body["thinking"] = {"type": "adaptive"}
            changed = True

    mt = body.get("max_tokens")
    if not isinstance(mt, int) or mt < MAX_TOKENS_FLOOR:
        body["max_tokens"] = MAX_TOKENS_FLOOR
        changed = True

    if INJECT_REMINDER and not _system_has_reminder(body.get("system")):
        _inject_reminder(body)
        changed = True

    if changed:
        vlog("rewrote model=%s -> %s effort=%s max_tokens=%s"
             % (model_before, body.get("model"),
                body.get("output_config", {}).get("effort"),
                body.get("max_tokens")))
        return json.dumps(body).encode("utf-8"), route
    return raw, route


# --------------------------------------------------------------------------
# Anthropic <-> OpenAI translation (with tool-calling)
# --------------------------------------------------------------------------

def _text_from_anthropic_content(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_result":
                parts.append(_text_from_anthropic_content(block.get("content")))
            elif btype == "image":
                parts.append("[image omitted]")
    return "\n".join(p for p in parts if p)


def _oai_image_url_from_anthropic_source(source) -> str:
    """Anthropic image `source` -> an OpenAI-style image URL string. A base64
    source becomes a data: URL; a url source is passed through. "" if unusable."""
    if not isinstance(source, dict):
        return ""
    stype = source.get("type")
    if stype == "base64":
        data = source.get("data") or ""
        if not data:
            return ""
        return "data:%s;base64,%s" % (source.get("media_type") or "image/png", data)
    if stype == "url":
        return source.get("url") or ""
    return ""


def _content_has_image(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "image" for b in content)


def _anthropic_content_to_oai(content):
    """OpenAI chat `content` for a user message. Returns a plain string when the
    content is text-only (the overwhelmingly common case -- behavior unchanged),
    or a list of typed parts ({"type":"text"} / {"type":"image_url"}) when image
    blocks are present, so vision-capable backends actually receive the image
    instead of the "[image omitted]" stub."""
    if not _content_has_image(content):
        return content if isinstance(content, str) else _text_from_anthropic_content(content)
    parts = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text") or ""
            if txt:
                parts.append({"type": "text", "text": txt})
        elif btype == "image":
            url = _oai_image_url_from_anthropic_source(block.get("source"))
            parts.append({"type": "image_url", "image_url": {"url": url}} if url
                         else {"type": "text", "text": "[image omitted]"})
        elif btype == "tool_result":
            txt = _text_from_anthropic_content(block.get("content"))
            if txt:
                parts.append({"type": "text", "text": txt})
    return parts or ""


def _toolresult_text_and_images(content):
    """Split an Anthropic tool_result's content into (text, [OpenAI image_url
    parts]). Tool-role messages can't carry images on OpenAI/codex backends, so
    callers re-send the images in a following user message instead of dropping
    them — keeps computer-use / screenshot / image tool output visible to vision
    models."""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return _text_from_anthropic_content(content), []
    texts, images = [], []
    for b in content:
        if isinstance(b, str):
            if b:
                texts.append(b)
        elif isinstance(b, dict):
            bt = b.get("type")
            if bt == "text":
                if b.get("text"):
                    texts.append(b["text"])
            elif bt == "image":
                url = _oai_image_url_from_anthropic_source(b.get("source"))
                if url:
                    images.append({"type": "image_url", "image_url": {"url": url}})
    return "\n".join(texts), images


def _anthropic_tools_to_oai(tools):
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out


def _anthropic_tool_choice_to_oai(tc):
    """Map Anthropic tool_choice to OpenAI tool_choice."""
    if not isinstance(tc, dict):
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    if t == "none":
        return "none"
    return None


def anthropic_to_openai(body: dict) -> dict:
    """Convert an Anthropic /v1/messages body to an OpenAI chat-completions
    body, preserving tools, tool calls and tool results."""
    messages = []

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        sys_txt = _text_from_anthropic_content(system)
        if sys_txt.strip():
            messages.append({"role": "system", "content": sys_txt})

    # OpenAI (and strict backends like DeepSeek) require every assistant message
    # that carries tool_calls to be IMMEDIATELY followed by exactly one `tool`
    # message per tool_call_id. Claude Code breaks that in two ways: (a) when a
    # call is rejected-with-a-comment it puts the user's text alongside/ahead of
    # the tool_result, and (b) for a rejected or unselected parallel call it sends
    # no result at all. Both yield "insufficient tool messages following tool_calls
    # message" (issue #3). We track the ids awaiting a reply, always emit the tool
    # replies FIRST, and synthesize a stub for any id the client didn't answer.
    pending_tool_ids = []  # mutated in place (never rebound) so the closure stays valid

    # Emit a single `tool` reply; if the result carried image(s) (a screenshot
    # etc.), put the text in the reply and append the images to `carried` as
    # OpenAI parts — tool-role messages can't hold images, so they ride along in
    # the user message that follows the tool replies.
    def _tool_reply(tid, tr, carried):
        text, imgs = _toolresult_text_and_images(tr.get("content"))
        if imgs and not text:
            text = "(image output is in the next message)"
        messages.append({"role": "tool", "tool_call_id": tid or "call_unknown",
                         "content": text or "(no output)"})
        if imgs:
            carried.append({"type": "text",
                            "text": "[image output from tool call %s]" % (tid or "call_unknown")})
            carried.extend(imgs)

    def _flush_tool_replies(by_id):
        carried = []
        for tid in pending_tool_ids:
            tr = by_id.pop(tid, None)
            if tr is not None:
                _tool_reply(tid, tr, carried)
            else:
                messages.append({
                    "role": "tool", "tool_call_id": tid,
                    "content": "Tool call was not executed (rejected or skipped by the user).",
                })
        # Stray results that didn't match a pending id (unusual) — keep them anyway.
        for tid, tr in by_id.items():
            _tool_reply(tid, tr, carried)
        del pending_tool_ids[:]
        return carried

    for m in body.get("messages", []):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")

        # Pending tool calls are only legitimately answered by the NEXT user turn.
        # If any other message comes first, flush stubs so the tool_calls message
        # isn't left bare (which strict backends reject).
        if pending_tool_ids and role != "user":
            _flush_tool_replies({})

        if role == "assistant" and isinstance(content, list):
            text_parts = []
            tool_calls = []
            for block in content:
                if not isinstance(block, dict):
                    if block:
                        text_parts.append(str(block))
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    })
            text = "\n".join(p for p in text_parts if p)
            entry = {"role": "assistant"}
            if tool_calls:
                entry["tool_calls"] = tool_calls
                entry["content"] = text if text else None
                pending_tool_ids[:] = [tc["id"] for tc in tool_calls]
            else:
                entry["content"] = text
            messages.append(entry)
            continue

        if role == "user":
            if isinstance(content, list):
                tool_results = [b for b in content
                                if isinstance(b, dict) and b.get("type") == "tool_result"]
                text_blocks = [b for b in content
                               if not (isinstance(b, dict) and b.get("type") == "tool_result")]
                user_content = _anthropic_content_to_oai(text_blocks)
            else:
                tool_results = []
                user_content = _anthropic_content_to_oai(content)

            # 1. Tool replies FIRST — immediately after the assistant's tool_calls,
            #    in tool_call order, stubbing any the client left unanswered. Image
            #    output inside a tool_result is carried out (tool messages can't
            #    hold images) to ride along in the user message below.
            carried = []
            if pending_tool_ids:
                by_id = {}
                for tr in tool_results:
                    by_id.setdefault(tr.get("tool_use_id") or "call_unknown", tr)
                carried = _flush_tool_replies(by_id)
            else:
                for tr in tool_results:
                    _tool_reply(tr.get("tool_use_id"), tr, carried)

            # 2. THEN the user's own content, prefixed by any images the tools
            #    returned (e.g. a screenshot) so vision models actually see them.
            #    Folding both into one user message right after the tool replies
            #    keeps strict OpenAI-compatible backends happy.
            if isinstance(user_content, list):
                combined = carried + user_content
            elif user_content:  # non-empty string
                combined = carried + [{"type": "text", "text": user_content}] if carried else user_content
            else:
                combined = carried
            if combined:
                messages.append({"role": "user", "content": combined})
            continue

        # Plain string content or unexpected role.
        if role not in ("user", "assistant", "system", "tool"):
            role = "user"
        messages.append({"role": role, "content": _text_from_anthropic_content(content)})

    # Transcript ended with tool calls still open (rare) — stub them so the final
    # assistant tool_calls message stays valid for OpenAI-compatible backends.
    if pending_tool_ids:
        _flush_tool_replies({})

    out = {
        "model": body.get("model"),
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }
    tools = _anthropic_tools_to_oai(body.get("tools"))
    if tools:
        out["tools"] = tools
        choice = _anthropic_tool_choice_to_oai(body.get("tool_choice"))
        if choice is not None:
            out["tool_choice"] = choice
    mt = body.get("max_tokens")
    if isinstance(mt, int) and mt > 0:
        out["max_tokens"] = mt
    temp = body.get("temperature")
    if isinstance(temp, (int, float)):
        out["temperature"] = temp
    return out


def _new_msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _parse_tool_input(raw):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {"input": raw or ""}


# ---- internal event vocabulary -------------------------------------------
# Streamers below produce a sequence of small dict events so the codex and
# openai_compat paths can share one Anthropic-SSE emitter:
#   {"type": "text_delta", "text": "..."}
#   {"type": "tool_call", "id": "...", "name": "...", "arguments": "<json str>"}
#   {"type": "usage", "input_tokens": N, "output_tokens": N}
#   {"type": "error", "message": "...", "status": N}


def _oai_response_to_events(resp):
    """Yield internal events from an OpenAI response (SSE or plain JSON)."""
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        pending = {}   # index -> {"id","name","args"}
        buf = b""
        finished_usage = None
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line or not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    buf = b""
                    break
                try:
                    obj = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                try:
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield {"type": "text_delta", "text": piece}
                    # Reasoning models (MiniMax-M3, DeepSeek-R*, ...) stream their
                    # chain-of-thought under reasoning_content before the first
                    # answer token. We keep it OUT of the answer, but surface it as
                    # a reasoning_delta so the proxy can keep the pipe alive instead
                    # of showing "dead air" while the model thinks.
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        yield {"type": "reasoning_delta", "text": rc}
                    for tc in (delta.get("tool_calls") or []):
                        idx = int(tc.get("index", 0))
                        slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        ac = fn.get("arguments")
                        if isinstance(ac, str):
                            slot["args"] += ac
                except Exception:
                    continue
                if isinstance(obj.get("usage"), dict):
                    u = obj["usage"]
                    finished_usage = {
                        "input_tokens": u.get("prompt_tokens", 0) or 0,
                        "output_tokens": u.get("completion_tokens", 0) or 0,
                    }
        for idx in sorted(pending):
            slot = pending[idx]
            if slot["name"] or slot["args"]:
                yield {"type": "tool_call", "id": slot["id"],
                       "name": slot["name"], "arguments": slot["args"] or "{}"}
        if finished_usage:
            yield {"type": "usage", **finished_usage}
        return

    # Plain JSON response.
    try:
        oai = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        yield {"type": "error", "message": "bad upstream JSON: %s" % e, "status": 502}
        return
    choice = (oai.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    if text:
        yield {"type": "text_delta", "text": text}
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        yield {"type": "tool_call", "id": tc.get("id") or "",
               "name": fn.get("name") or "", "arguments": fn.get("arguments") or "{}"}
    usage = oai.get("usage") or {}
    yield {"type": "usage",
           "input_tokens": usage.get("prompt_tokens", 0) or 0,
           "output_tokens": usage.get("completion_tokens", 0) or 0}


def _sse(event: str, data: dict) -> bytes:
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode("utf-8")


def _stop_reason_for(finish: str) -> str:
    return {"tool_calls": "tool_use", "length": "max_tokens",
            "stop": "end_turn"}.get(finish, "end_turn")


# --------------------------------------------------------------------------
# Empty-turn resilience
# --------------------------------------------------------------------------
# Some upstreams occasionally return a turn with NO text and NO tool call -- a
# transient hiccup, or a budget-exhausted (response.incomplete) reasoning turn at
# high effort (notably GPT-5.5 via codex). An empty assistant turn is useless, so
# we transparently retry a fresh turn a bounded number of times. Streaming is
# preserved: events are buffered only until the first meaningful event, so a normal
# turn has zero added latency and partial output is never duplicated.
EMPTY_RETRY_ATTEMPTS = int(os.environ.get("UC_EMPTY_RETRY_ATTEMPTS", "2"))
EMPTY_RETRY_BACKOFF = float(os.environ.get("UC_EMPTY_RETRY_BACKOFF", "0.75"))


def _retryable_status(status) -> bool:
    """Transient failures worth retrying; fatal (4xx auth/validation) are not."""
    try:
        s = int(status)
    except (TypeError, ValueError):
        return True  # unknown/None -> treat as transient
    return s == 0 or s >= 500 or s in (408, 409, 425, 429)


def _events_with_retry(make_events, attempts=EMPTY_RETRY_ATTEMPTS,
                       backoff=EMPTY_RETRY_BACKOFF, label="upstream"):
    """Yield events from make_events() (a zero-arg factory returning a FRESH event
    generator). If a turn yields no meaningful output (no non-whitespace text and
    no tool_call) and ended empty or with a retryable error, retry a fresh turn up
    to `attempts` times. Never retries after meaningful output, a fatal
    (non-retryable) error, or partial output already streamed."""
    last_buffer = []
    for attempt in range(attempts + 1):
        buffer = []
        meaningful = False
        fatal = None
        try:
            for ev in make_events():
                if meaningful:
                    yield ev
                    continue
                et = ev.get("type")
                if (et == "text_delta" and (ev.get("text") or "").strip()) or et == "tool_call":
                    meaningful = True
                    for b in buffer:
                        yield b
                    buffer = []
                    yield ev
                    continue
                if et == "error" and not _retryable_status(ev.get("status")):
                    fatal = ev
                buffer.append(ev)
        except Exception as e:
            vlog("%s stream error (attempt %d): %s" % (label, attempt + 1, e))
        if meaningful:
            return
        if fatal is not None:
            for b in buffer:
                yield b
            return
        last_buffer = buffer
        if attempt < attempts:
            log("%s: empty turn, retrying (%d/%d)" % (label, attempt + 1, attempts))
            time.sleep(backoff * (attempt + 1))
            continue
        for b in last_buffer:
            yield b
        return


# --------------------------------------------------------------------------
# Auto Router -- pick the right backend for each task, automatically
# --------------------------------------------------------------------------
# A "claude-auto" picker entry routes through a small, cheap *classifier* model
# (one of your configured backends) that scores every candidate backend on how
# likely it is to nail THIS task on the first try (0.0-1.0). The proxy then sends
# the real request to the CHEAPEST candidate whose score clears a quality bar
# (default 0.7) -- so trivial turns go to a cheap model and hard turns escalate
# to your strongest one, automatically. Same "classifier picks the model" idea
# popularized by tools like Factory Droid's router, built on the models you
# already pay for.
#
# Design notes:
#   * The classifier scores on CAPABILITY only; it never sees price. Cost is
#     applied afterward by the selector's cheapest-among-viable tie-break, so the
#     classifier can't be biased toward expensive models.
#   * Every decision is cached per task (keyed by the user's message) so a task's
#     follow-up tool-call round-trips reuse one classification instead of paying
#     the classifier tax on every request.
#   * Every decision is logged (UC_ROUTER_LOG=1) -- the cheap, honest way to learn
#     whether routing is actually helping, which a feedback loop would later use.
#   * It degrades safely at every step: unknown candidate ids are dropped, a
#     missing/again classifier falls back to the cheapest candidate deterministically,
#     and any error falls back to the configured default. The request never breaks.

_ROUTER_CACHE = {}          # key -> candidate id
_ROUTER_CACHE_LOCK = threading.Lock()
_ROUTER_CACHE_MAX = 256


def _router_is_enabled():
    return bool(ROUTER.get("enabled")) and ROUTER_ENABLED_ENV


def _router_available_candidates():
    """Candidates whose backend is actually configured (route among available
    models only). Keeps routing working even if the user deleted some examples."""
    out = []
    for c in ROUTER.get("candidates") or []:
        cid = c.get("id")
        if cid and cid in UC_SLOT_MAP and UC_SLOT_MAP[cid].get("type") != "auto":
            out.append(c)
    return out


def _clamp01(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _last_user_text(anth_body):
    """Sanitized text of the latest user turn -- the task to classify. A turn that
    is ONLY tool_result blocks is a tool round-trip, not a fresh ask, so it is
    skipped (keeps the router cache key on the real instruction)."""
    return _latest_user_turn(anth_body)[1]


def _has_images(anth_body):
    for m in anth_body.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image":
                    return True
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    inner = b.get("content")
                    if isinstance(inner, list):
                        for ib in inner:
                            if isinstance(ib, dict) and ib.get("type") == "image":
                                return True
    return False


def _router_signal(anth_body, tier):
    """The compact task description handed to the classifier."""
    msgs = anth_body.get("messages") or []
    user_turns = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")
    tool_names = []
    for t in anth_body.get("tools") or []:
        if isinstance(t, dict) and t.get("name"):
            tool_names.append(t["name"])
    return {
        "task": _last_user_text(anth_body),
        "surface": "orchestrator" if tier == "heavy" else "worker/sub-agent",
        "has_images": _has_images(anth_body),
        "turns": user_turns,
        "tool_count": len(tool_names),
    }


def _router_system_prompt(candidates):
    """Build the classifier instructions + capability cards + output schema."""
    lines = [
        "You are a task-routing classifier for an AI coding agent.",
        "You are given a <session> describing the user's current task and a list",
        "of candidate models. For EACH candidate, output a score from 0.0 to 1.0:",
        "the probability that the model completes THIS task correctly on its first",
        "attempt, without errors or rework.",
        "",
        "You are NOT choosing a winner. A downstream system combines your scores",
        "with cost data you do not see to make the final pick. Be an accurate,",
        "well-calibrated, independent probability estimator for each model.",
        "",
        "Scoring guide:",
        "  0.0       cannot attempt (e.g. images required but unsupported) -- exact 0.0",
        "  0.1-0.3   will almost certainly fail; lacks the capability",
        "  0.4-0.6   real chance of failure; touches a known weakness or is uncertain",
        "  0.7-0.8   likely success; handles this category well",
        "  0.9-1.0   near-certain success; well within demonstrated ability",
        "Use the full range. A short prompt is NOT necessarily an easy task -- hidden",
        "complexity (multi-file edits, debugging, niche domains, strict correctness)",
        "should pull scores down for weaker models. Default to ~0.5-0.6 when unsure.",
        "",
        "Candidate models:",
    ]
    for c in candidates:
        card = (c.get("card") or "").strip() or "General-purpose model. No capability card provided."
        imgs = "yes" if c.get("supports_images") else "no"
        lines.append("- id: %s" % c["id"])
        lines.append("  images: %s" % imgs)
        lines.append("  capability: %s" % card)
    schema = {"scores": {c["id"]: 0.0 for c in candidates},
              "reasoning": "one short sentence"}
    lines += [
        "",
        "Respond with ONE JSON object, no prose, no code fence, exactly this shape:",
        json.dumps(schema, ensure_ascii=False),
        "Every candidate id above MUST appear in \"scores\". Each value in [0.0, 1.0].",
    ]
    return "\n".join(lines)


def _router_user_content(signal, candidates):
    task = signal["task"] or "(no explicit instruction; infer from context)"
    if len(task) > 6000:               # head+tail, keep it cheap
        task = task[:3000] + "\n...\n" + task[-3000:]
    return "\n".join([
        "<session>",
        "  surface: %s" % signal["surface"],
        "  images_present: %s" % ("yes" if signal["has_images"] else "no"),
        "  user_turns: %d" % signal["turns"],
        "  tools_available: %d" % signal["tool_count"],
        "  current_task: |",
        "\n".join("    " + ln for ln in task.splitlines()) or "    (empty)",
        "</session>",
        "",
        "Score these candidate ids: %s" % ", ".join(c["id"] for c in candidates),
    ])


def _classifier_complete(slot, system_prompt, user_content, timeout):
    """Single, non-streaming completion against the classifier backend. Returns
    the response text (possibly with surrounding prose) or raises."""
    stype = slot.get("type")
    if stype == "codex_oauth":
        if _codex_oauth is None:
            raise RuntimeError("codex_oauth classifier needs providers/codex_oauth.py")
        text = []
        for ev in _codex_oauth.stream_events(
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_content}],
            tools=None, tool_choice=None, model=slot.get("model") or "gpt-5.5"):
            if ev.get("type") == "text_delta":
                text.append(ev.get("text") or "")
            elif ev.get("type") == "error":
                raise RuntimeError(ev.get("message") or "codex classifier error")
        return "".join(text)

    if stype == "openai_compat":
        url = (slot.get("upstream") or UPSTREAM).rstrip("/") + "/chat/completions"
        payload = {
            "model": slot.get("model"),
            "stream": False,
            "temperature": 0,
            "max_tokens": ROUTER_MAX_TOKENS,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_content}],
        }
        sbody = slot.get("body")
        if isinstance(sbody, dict):
            for bk, bv in sbody.items():
                payload[bk] = _expand_env(bv) if isinstance(bv, str) else bv
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Content-Length": str(len(data)), "User-Agent": BROWSER_UA,
                   "Accept-Language": "en-US,en;q=0.9"}
        auth = slot.get("auth")
        if auth and auth != "passthrough":
            Handler._apply_auth_header(headers, auth)
        else:
            headers["Authorization"] = "Bearer unused"
        for hk, hv in (slot.get("headers") or {}).items():
            headers[hk] = hv
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            obj = json.loads(resp.read().decode("utf-8"))
            msg = ((obj.get("choices") or [{}])[0].get("message") or {})
            return msg.get("content") or ""
        finally:
            resp.close()

    # Anthropic passthrough (real Claude or any Anthropic-compatible endpoint).
    url = (slot.get("upstream") or UPSTREAM).rstrip("/") + "/v1/messages"
    payload = {"model": slot.get("model"), "max_tokens": ROUTER_MAX_TOKENS,
               "system": system_prompt,
               "messages": [{"role": "user", "content": user_content}]}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Content-Length": str(len(data)),
               "anthropic-version": "2023-06-01", "User-Agent": BROWSER_UA}
    auth = slot.get("auth")
    if auth and auth != "passthrough":
        Handler._apply_auth_header(headers, auth)
    for hk, hv in (slot.get("headers") or {}).items():
        headers[hk] = hv
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    try:
        obj = json.loads(resp.read().decode("utf-8"))
        return _text_from_anthropic_content(obj.get("content"))
    finally:
        resp.close()


def _parse_scores(text, candidate_ids):
    """Pull the {"scores": {...}} object out of the classifier's reply."""
    if not text:
        return {}
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return {}
    for end in (e, text.find("}", s)):       # try greedy then first object
        if end == -1 or end <= s:
            continue
        try:
            obj = json.loads(text[s:end + 1])
        except Exception:
            continue
        scores = obj.get("scores") if isinstance(obj, dict) else None
        if isinstance(scores, dict):
            return {cid: _clamp01(scores.get(cid, 0)) for cid in candidate_ids}
    return {}


def _router_pick(scores, candidates, threshold, has_images):
    """Cheapest candidate whose score clears the bar; image-incapable models are
    hard-zeroed when the task has images; if none clear the bar, take the best."""
    scored = []
    for c in candidates:
        sc = scores.get(c["id"], 0.0)
        if has_images and not c.get("supports_images"):
            sc = 0.0
        scored.append((c, sc))
    viable = [(c, sc) for (c, sc) in scored if sc >= threshold]
    if viable:
        winner = min(viable, key=lambda cs: (float(cs[0].get("cost", 0) or 0), -cs[1]))
        return winner[0]["id"], winner[1], "score>=%.2f, cheapest" % threshold
    best = max(scored, key=lambda cs: cs[1], default=None)
    if best and best[1] > 0:
        return best[0]["id"], best[1], "below bar; highest score"
    return None, 0.0, "no usable score"


def _router_cache_key(signal, tier):
    # Stable across process restarts: Python's builtin hash() is salted per run
    # (PYTHONHASHSEED), so it would mint a different key for the same task every
    # restart, silently defeating the cache and re-running the classifier. (#20)
    digest = hashlib.sha256(signal["task"].encode("utf-8", "replace")).hexdigest()
    return "%s|%s" % (tier, digest[:32])


def _router_fallback_id(candidates):
    """Deterministic, classifier-free choice: configured default, else cheapest."""
    cfg_default = ROUTER.get("default")
    if cfg_default and any(c["id"] == cfg_default for c in candidates):
        return cfg_default
    if candidates:
        return min(candidates, key=lambda c: float(c.get("cost", 0) or 0))["id"]
    return None


def _configure_router(router_cfg):
    """Populate ROUTER from config.json's "router" block, and make sure the
    picker model id + its synthetic {"type":"auto"} route exist so it shows up in
    /model, /v1/models, and the pre-launch selector. Idempotent."""
    if not isinstance(router_cfg, dict):
        return
    rid = router_cfg.get("id") or "claude-auto"
    cands = []
    for c in router_cfg.get("candidates") or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        cands.append({
            "id": c["id"],
            "cost": float(c.get("cost", 1) or 0),
            "card": c.get("card") or "",
            "supports_images": bool(c.get("supports_images", False)),
        })
    ROUTER.update({
        "enabled": bool(router_cfg.get("enabled", False)),
        "id": rid,
        "classifier": router_cfg.get("classifier"),
        "threshold": float(router_cfg.get("threshold", 0.7) or 0.7),
        "candidates": cands,
        "default": router_cfg.get("default"),
        "cache": bool(router_cfg.get("cache", True)),
    })
    if not ROUTER["enabled"]:
        return
    if not isinstance(UC_SLOT_MAP.get(rid), dict):
        UC_SLOT_MAP[rid] = {"type": "auto"}
    else:
        UC_SLOT_MAP[rid]["type"] = "auto"
    if not any(m.get("id") == rid for m in UC_MODELS):
        UC_MODELS.insert(0, {
            "type": "model", "id": rid,
            "display_name": router_cfg.get("display_name") or "Auto (smart routing)",
            "created_at": "2025-01-01T00:00:00Z",
        })


def resolve_auto(anth_body, tier):
    """Return the concrete candidate model id the Auto Router selects for this
    request, or None to leave routing unchanged. Never raises."""
    try:
        candidates = _router_available_candidates()
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]["id"]

        signal = _router_signal(anth_body, tier)
        has_images = signal["has_images"]
        key = _router_cache_key(signal, tier)
        if ROUTER.get("cache"):
            with _ROUTER_CACHE_LOCK:
                cached = _ROUTER_CACHE.get(key)
            if cached and any(c["id"] == cached for c in candidates):
                if ROUTER_LOG:
                    log("[router] tier=%s cache-hit -> %s" % (tier, cached))
                return cached

        classifier_id = ROUTER.get("classifier")
        classifier_slot = UC_SLOT_MAP.get(classifier_id) if classifier_id else None
        if not isinstance(classifier_slot, dict) or classifier_slot.get("type") == "auto":
            pick = _router_fallback_id(candidates)
            if ROUTER_LOG:
                log("[router] tier=%s no classifier -> deterministic %s" % (tier, pick))
            return pick

        system_prompt = _router_system_prompt(candidates)
        user_content = _router_user_content(signal, candidates)
        try:
            raw = _classifier_complete(classifier_slot, system_prompt, user_content,
                                       ROUTER_TIMEOUT)
        except Exception as e:
            pick = _router_fallback_id(candidates)
            log("[router] classifier failed (%s); falling back to %s" % (e, pick))
            return pick

        scores = _parse_scores(raw, [c["id"] for c in candidates])
        threshold = float(ROUTER.get("threshold", 0.7))
        pick, score, why = _router_pick(scores, candidates, threshold, has_images)
        if not pick:
            pick = _router_fallback_id(candidates)
            why = "empty scores; fallback"
            score = 0.0
        if ROUTER.get("cache") and pick:
            with _ROUTER_CACHE_LOCK:
                if len(_ROUTER_CACHE) >= _ROUTER_CACHE_MAX:
                    _ROUTER_CACHE.clear()
                _ROUTER_CACHE[key] = pick
        if ROUTER_LOG or VERBOSE:
            log("[router] tier=%s -> %s (score=%.2f; %s) scores=%s"
                % (tier, pick, score, why,
                   json.dumps({c["id"]: round(scores.get(c["id"], 0.0), 2) for c in candidates})))
        return pick
    except Exception as e:
        vlog("[router] resolve_auto unexpected error: %s" % e)
        return None


# --------------------------------------------------------------------------
# HTTP proxy
# --------------------------------------------------------------------------

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "accept-encoding",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ultracode-proxy/2.0"
    timeout = SOCKET_TIMEOUT if SOCKET_TIMEOUT > 0 else None  # bound idle sockets (#24)

    def log_message(self, fmt, *args):
        if VERBOSE:
            log("http " + (fmt % args))

    # ---- health ----------------------------------------------------------
    def _maybe_health(self) -> bool:
        if self.path in ("/healthz", "/health"):
            payload = json.dumps({
                "ok": True,
                "upstream": UPSTREAM,
                "effort": FORCE_EFFORT,
                "max_tokens_floor": MAX_TOKENS_FLOOR,
                "inject_reminder": INJECT_REMINDER,
                "codex_helper": _codex_oauth is not None,
                "router": {
                    "enabled": _router_is_enabled(),
                    "id": ROUTER.get("id"),
                    "classifier": ROUTER.get("classifier"),
                    "threshold": ROUTER.get("threshold"),
                    "candidates": [{"id": c["id"], "cost": c.get("cost")}
                                   for c in _router_available_candidates()],
                },
                "orchestrator_worker": _orchestrator_worker_status(),
                "custom_models": [{"id": _advertise_id(m), "display_name": m["display_name"]}
                                  for m in UC_MODELS],
                "stock_models": [{"id": m["id"], "display_name": m["display_name"]}
                                 for m in _stock_models()],
                "stock_learning": {"enabled": LEARN_STOCK_MODELS,
                                   "learned": len(_LEARNED_STOCK),
                                   "cache": _stock_cache_path()},
                "slots": {k: {"type": v.get("type", "passthrough"),
                              "model": v.get("model"),
                              "upstream": v.get("upstream", "(default)")}
                          for k, v in UC_SLOT_MAP.items()},
            }).encode("utf-8")
            self._raw(200, "application/json", payload)
            return True
        return False

    def _guard_local(self) -> bool:
        """When bound to loopback, serve only loopback-origin requests (anti
        DNS-rebinding). Returns True if allowed; sends 403 and returns False
        otherwise. A non-loopback UC_LISTEN_HOST opts out. (#19)"""
        if not (GUARD_LOCAL and _is_loopback_listen()):
            return True
        if _request_host_is_local(self.headers.get("Host")):
            return True
        self.close_connection = True
        self._raw(403, "application/json", json.dumps(
            {"type": "error", "error": {"type": "forbidden",
             "message": "ultracode-proxy: refusing non-local Host header "
                        "(proxy is bound to loopback)"}}).encode("utf-8"))
        return False

    def do_GET(self):
        if not self._guard_local():
            return
        if self._maybe_health():
            return
        if self.path.split("?")[0] == "/uc/select":
            self._handle_uc_select_get()
            return
        if self.path.split("?")[0].endswith("/v1/models") and self._handle_models():
            return
        self._proxy("GET")

    def do_POST(self):
        if not self._guard_local():
            return
        if self.path.split("?")[0] == "/uc/select":
            self._handle_uc_select_post()
            return
        self._proxy("POST")

    # ---- /uc/select: pre-set orchestrator+worker (used by the launcher TUI) ----
    def _handle_uc_select_get(self):
        with _SEL_LOCK:
            active = dict(_ACTIVE)
        self._raw(200, "application/json", json.dumps({
            "ok": True,
            "active": active,
            "orchestrators": [{"id": m["id"], "display_name": m["display_name"]}
                              for m in UC_MODELS if m["id"] in _ORCH_PICK_IDS],
            "workers": [{"id": wid, "base": base,
                         "display_name": next((m["display_name"] for m in UC_MODELS
                                               if m["id"] == wid), wid)}
                        for wid, base in _WORKER_MAP.items()],
        }).encode("utf-8"))

    def _handle_uc_select_post(self):
        body = self._read_body()
        if body is None:
            return
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception as e:
            self._raw(400, "application/json",
                      json.dumps({"ok": False, "error": "bad json: %s" % e}).encode("utf-8"))
            return
        worker = data.get("worker")
        if worker in _WORKER_MAP:      # accept either a worker picker id or its base
            worker = _WORKER_MAP[worker]
        active = _set_selection(orch=data.get("orchestrator"), worker=worker)
        log("uc/select set orchestrator=%s worker=%s" % (active.get("orch"), active.get("worker")))
        self._raw(200, "application/json",
                  json.dumps({"ok": True, "active": active}).encode("utf-8"))

    def do_PUT(self):
        if not self._guard_local():
            return
        self._proxy("PUT")

    def do_DELETE(self):
        if not self._guard_local():
            return
        self._proxy("DELETE")

    # ---- /v1/models discovery -------------------------------------------
    def _handle_models(self) -> bool:
        # We answer /v1/models whenever there's anything to add to (or stand in
        # for) the upstream list: your configured models OR the built-in stock
        # Claude models. Only when both are empty do we let the request pass
        # straight through unchanged.
        stock = _stock_models()
        if not UC_MODELS and not stock:
            return False
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP}
        fwd_headers["Accept-Encoding"] = "identity"
        fwd_headers.setdefault("User-Agent", BROWSER_UA)
        url = UPSTREAM + self.path
        base = {"data": [], "has_more": False, "first_id": None, "last_id": None}
        try:
            req = urllib.request.Request(url, headers=fwd_headers, method="GET")
            resp = urllib.request.urlopen(req, timeout=30)
            try:
                parsed = json.loads(resp.read().decode("utf-8"))
                if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
                    base = parsed
                    # Learn the current real-Claude ids from this successful fetch and
                    # cache them, so a newly released Opus is remembered for next time
                    # (even when upstream is later unreachable). Recompute stock after
                    # learning so any new id also lands in THIS response.
                    _learn_stock_from_upstream(base["data"])
                    stock = _stock_models()
            finally:
                resp.close()
        except Exception as e:
            # No usable upstream list (e.g. no Anthropic credential to forward,
            # or an offline blip). We still serve stock + custom below, so real
            # Claude and your models stay visible instead of vanishing.
            vlog("/v1/models upstream fetch failed, serving stock+custom only: %s" % e)
        data = base.get("data")
        if not isinstance(data, list):
            data = []
            base["data"] = data
        existing = {m.get("id") for m in data if isinstance(m, dict)}
        # Add stock (real Claude) first, then your configured models. Skip any id
        # already present. For stock we also skip by model *family*, so the
        # baseline's dateless id (claude-haiku-4-5) doesn't double up with an
        # upstream/learned dated id (claude-haiku-4-5-20251001) for one model.
        stock_families = {_model_family(m.get("id")) for m in data if isinstance(m, dict)}
        for m in stock:
            fam = _model_family(m["id"])
            if m["id"] in existing or fam in stock_families:
                continue
            data.append(dict(m))
            existing.add(m["id"])
            stock_families.add(fam)
        for m in UC_MODELS:
            adv = _advertise_id(m)
            if adv not in existing:
                data.append({**m, "id": adv})
                existing.add(adv)
        self._raw(200, "application/json", json.dumps(base).encode("utf-8"))
        return True

    # ---- core proxy ------------------------------------------------------
    def _read_body(self):
        """Read the request body, or None if it was rejected (a response was
        already sent). Bodies above MAX_BODY_BYTES are refused before allocation
        so a single oversized upload can't exhaust memory. (#24)"""
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        if MAX_BODY_BYTES and n > MAX_BODY_BYTES:
            self.close_connection = True
            self._raw(413, "application/json", json.dumps(
                {"type": "error", "error": {"type": "payload_too_large",
                 "message": "ultracode-proxy: request body of %d bytes exceeds "
                            "the %d-byte limit" % (n, MAX_BODY_BYTES)}}).encode("utf-8"))
            return None
        return self.rfile.read(n)

    def _proxy(self, method: str):
        body = self._read_body()
        if body is None:
            return
        is_messages = self.path.split("?")[0].endswith("/v1/messages")
        route = {}
        if is_messages and method == "POST" and body:
            body, route = transform_messages_body(body)

        rtype = route.get("type")
        if is_messages and method == "POST":
            if rtype == "openai_compat":
                self._handle_openai_compat(body, route)
                return
            if rtype == "codex_oauth":
                self._handle_codex(body, route)
                return
            if rtype == "cursor_agent":
                self._handle_cursor_agent(body, route)
                return

        # Anthropic passthrough.
        upstream = route.get("upstream") or UPSTREAM
        url = upstream + self.path
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP}
        # Don't leak Claude Code's own Anthropic credential to a custom upstream
        # unless the route explicitly opts into passthrough auth. (#22)
        if upstream != UPSTREAM and not route.get("auth_passthrough"):
            for hk in [h for h in fwd_headers if h.lower() in _INBOUND_CRED_HEADERS]:
                del fwd_headers[hk]
        auth_override = route.get("auth")
        if auth_override:
            self._apply_auth_header(fwd_headers, auth_override)
        for hk, hv in (route.get("headers") or {}).items():
            fwd_headers[hk] = hv
        fwd_headers["Accept-Encoding"] = "identity"
        fwd_headers.setdefault("User-Agent", BROWSER_UA)
        if body:
            fwd_headers["Content-Length"] = str(len(body))
        # For a streaming /v1/messages turn, surface a transport-level failure
        # (timeout, connection refused) as visible assistant text instead of an
        # opaque error Claude Code can't render. (#18)
        want_stream, surface_id = False, None
        if is_messages and method == "POST" and body:
            try:
                _b = json.loads(body.decode("utf-8"))
                want_stream = bool(_b.get("stream", False))
                surface_id = _b.get("model")
            except Exception:
                pass
        req = urllib.request.Request(url, data=body or None,
                                     headers=fwd_headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            self._relay_response(e, streaming=False)
            return
        except Exception as e:
            log("upstream error %s for %s" % (e, url))
            msg = "upstream error talking to %s: %s" % (upstream, e)
            if is_messages and method == "POST":
                self._emit_or_error(want_stream, surface_id or "claude", 502, msg)
            else:
                self._send_error(502, msg)
            return
        ctype = resp.headers.get("Content-Type", "")
        self._relay_response(resp, streaming="text/event-stream" in ctype)

    @staticmethod
    def _apply_auth_header(headers: dict, auth: str):
        if ":" in auth and not auth.lower().startswith("bearer"):
            hk, hv = auth.split(":", 1)
            headers[hk.strip()] = hv.strip()
        else:
            headers["Authorization"] = auth

    # ---- openai_compat backend ------------------------------------------
    def _handle_openai_compat(self, body: bytes, route: dict):
        try:
            anth = json.loads(body.decode("utf-8"))
        except Exception as e:
            self._send_error(400, "openai_compat: bad request body: %s" % e)
            return
        model_id = anth.get("model")
        want_stream = bool(anth.get("stream", False))
        oai_body = anthropic_to_openai(anth)
        # The UltraCode envelope forces a large max_tokens for Anthropic, but many
        # OpenAI-compatible backends reject a completion cap that big. Use a safe
        # default (8192) unless the slot overrides it with max_output_tokens.
        cap = route.get("max_output_tokens")
        oai_body["max_tokens"] = int(cap) if cap else 8192
        # Optional per-route extra body params merged into the OpenAI request.
        # Lets a backend get provider-specific flags it needs, e.g. MiniMax-M3's
        # "reasoning_split": true (keeps the model's <think> chain-of-thought out
        # of the visible answer). Values support ${ENV} expansion.
        extra_body = route.get("body")
        if isinstance(extra_body, dict):
            for bk, bv in extra_body.items():
                oai_body[bk] = _expand_env(bv) if isinstance(bv, str) else bv
        payload = json.dumps(oai_body).encode("utf-8")

        # upstream is the OpenAI-compatible base URL you'd copy from the provider's
        # docs (usually ends in /v1); we append /chat/completions.
        upstream = (route.get("upstream") or UPSTREAM).rstrip("/")
        url = upstream + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if want_stream else "application/json",
            "Content-Length": str(len(payload)),
            "User-Agent": BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        }
        auth_override = route.get("auth")
        if auth_override and auth_override != "passthrough":
            self._apply_auth_header(headers, auth_override)
        else:
            headers["Authorization"] = "Bearer unused"
        for hk, hv in (route.get("headers") or {}).items():
            headers[hk] = hv

        vlog("openai_compat -> %s model=%s stream=%s" % (url, model_id, want_stream))

        def _mk_events():
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            resp = None
            try:
                try:
                    resp = urllib.request.urlopen(req, timeout=600)
                except urllib.error.HTTPError as e:
                    detail = ""
                    try:
                        detail = e.read().decode("utf-8", "replace")[:800]
                    except Exception:
                        pass
                    hint = _context_length_hint(detail)
                    log("openai_compat upstream HTTP %s for %s: %s" % (e.code, url, detail))
                    yield {"type": "error", "status": e.code,
                           "message": "openai_compat upstream %s: %s%s"
                           % (e.code, detail, hint)}
                    return
                except Exception as e:
                    log("openai_compat upstream error %s for %s" % (e, url))
                    yield {"type": "error", "status": 502,
                           "message": "openai_compat upstream error: %s" % e}
                    return
                yield from _oai_response_to_events(resp)
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass

        events = _events_with_retry(_mk_events, label="openai_compat %s" % model_id)
        if want_stream:
            self._stream_anthropic_from_events(events, model_id)
        else:
            self._json_anthropic_from_events(events, model_id)

    # ---- codex_oauth backend --------------------------------------------
    def _handle_codex(self, body: bytes, route: dict):
        if _codex_oauth is None:
            self._send_error(
                501,
                "codex_oauth route requires providers/codex_oauth.py and a "
                "ChatGPT/Codex login (run: codex login). See docs/ADD_A_MODEL.md.")
            return
        try:
            anth = json.loads(body.decode("utf-8"))
        except Exception as e:
            self._send_error(400, "codex: bad request body: %s" % e)
            return
        model_id = anth.get("model") or route.get("model") or "gpt-5.5"
        want_stream = bool(anth.get("stream", False))
        oai_body = anthropic_to_openai(anth)
        events = _events_with_retry(
            lambda: _codex_oauth.stream_events(
                messages=oai_body.get("messages") or [],
                tools=oai_body.get("tools"),
                tool_choice=oai_body.get("tool_choice"),
                model=route.get("model") or model_id,
            ),
            label="codex %s" % model_id,
        )
        if want_stream:
            self._stream_anthropic_from_events(events, model_id)
        else:
            self._json_anthropic_from_events(events, model_id)

    # ---- cursor_agent backend (Cursor Composer via the cursor-agent CLI) ----
    def _handle_cursor_agent(self, body: bytes, route: dict):
        if _cursor_agent is None:
            self._send_error(
                501,
                "cursor_agent route requires providers/cursor_agent.py and the "
                "cursor-agent CLI (cursor-agent login). See docs/ADD_A_MODEL.md.")
            return
        try:
            anth = json.loads(body.decode("utf-8"))
        except Exception as e:
            self._send_error(400, "cursor_agent: bad request body: %s" % e)
            return
        model_id = anth.get("model") or route.get("model") or "composer-2.5"
        want_stream = bool(anth.get("stream", False))
        oai_body = anthropic_to_openai(anth)
        try:
            events = _cursor_agent.stream_events(
                messages=oai_body.get("messages") or [],
                tools=oai_body.get("tools"),
                model=route.get("model") or "composer-2.5",
                workspace=route.get("workspace"),
            )
        except Exception as e:
            log("cursor_agent helper error: %s" % e)
            self._emit_or_error(want_stream, model_id, 502, "cursor_agent helper error: %s" % e)
            return
        if want_stream:
            self._stream_anthropic_from_events(events, model_id)
        else:
            self._json_anthropic_from_events(events, model_id)

    def _emit_or_error(self, want_stream, model_id, status, message):
        if want_stream:
            self._stream_anthropic_from_events(
                iter([{"type": "error", "message": message, "status": status}]), model_id)
        else:
            self._send_error(status, message)

    # ---- shared Anthropic emitters --------------------------------------
    def _stream_anthropic_from_events(self, events, model_id: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.close_connection = True
        self.send_header("Connection", "close")
        self.end_headers()

        msg_id = _new_msg_id()
        self.wfile.write(_sse("message_start", {
            "type": "message_start",
            "message": {"id": msg_id, "type": "message", "role": "assistant",
                        "model": model_id, "content": [],
                        "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}}}))
        self.wfile.flush()

        index = 0
        text_open = False
        emitted = False
        stop_reason = "end_turn"
        out_tok = 0

        def open_text():
            nonlocal text_open
            self.wfile.write(_sse("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "text", "text": ""}}))
            text_open = True

        def close_block():
            self.wfile.write(_sse("content_block_stop",
                                  {"type": "content_block_stop", "index": index}))

        try:
            for ev in events:
                et = ev.get("type")
                if et == "text_delta":
                    txt = ev.get("text") or ""
                    if not txt:
                        continue
                    if not text_open:
                        open_text()
                    self.wfile.write(_sse("content_block_delta", {
                        "type": "content_block_delta", "index": index,
                        "delta": {"type": "text_delta", "text": txt}}))
                    self.wfile.flush()
                    emitted = True
                elif et == "reasoning_delta":
                    # Keep the pipe alive while a reasoning model thinks (no dead
                    # air), without leaking the chain-of-thought into the answer.
                    self.wfile.write(_sse("ping", {"type": "ping"}))
                    self.wfile.flush()
                elif et == "tool_call":
                    if text_open:
                        close_block()
                        index += 1
                        text_open = False
                    self.wfile.write(_sse("content_block_start", {
                        "type": "content_block_start", "index": index,
                        "content_block": {"type": "tool_use",
                                          "id": ev.get("id") or ("toolu_" + uuid.uuid4().hex[:16]),
                                          "name": ev.get("name") or "", "input": {}}}))
                    self.wfile.write(_sse("content_block_delta", {
                        "type": "content_block_delta", "index": index,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": ev.get("arguments") or "{}"}}))
                    close_block()
                    index += 1
                    emitted = True
                    stop_reason = "tool_use"
                elif et == "usage":
                    out_tok = ev.get("output_tokens", out_tok) or out_tok
                elif et == "error":
                    if not emitted:
                        if not text_open:
                            open_text()
                        self.wfile.write(_sse("content_block_delta", {
                            "type": "content_block_delta", "index": index,
                            "delta": {"type": "text_delta",
                                      "text": "[ultracode-proxy] " + (ev.get("message") or "upstream error")}}))
                        emitted = True
                    break
            if text_open:
                close_block()
            elif not emitted:
                # Never sent a block; emit an empty text block so Claude Code
                # gets a well-formed turn.
                open_text()
                close_block()
        except Exception as e:
            vlog("anthropic stream relay ended: %s" % e)

        self.wfile.write(_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": out_tok}}))
        self.wfile.write(_sse("message_stop", {"type": "message_stop"}))
        self.wfile.flush()

    def _json_anthropic_from_events(self, events, model_id: str):
        full_text = ""
        tool_blocks = []
        in_tok = 0
        out_tok = 0
        err = None
        for ev in events:
            et = ev.get("type")
            if et == "text_delta":
                full_text += ev.get("text") or ""
            elif et == "tool_call":
                tool_blocks.append({
                    "type": "tool_use",
                    "id": ev.get("id") or ("toolu_" + uuid.uuid4().hex[:16]),
                    "name": ev.get("name") or "",
                    "input": _parse_tool_input(ev.get("arguments") or "{}"),
                })
            elif et == "usage":
                in_tok = max(in_tok, ev.get("input_tokens", 0) or 0)
                out_tok = max(out_tok, ev.get("output_tokens", 0) or 0)
            elif et == "error" and not full_text and not tool_blocks:
                err = ev
        if err is not None:
            self._send_error(int(err.get("status") or 502), err.get("message") or "upstream error")
            return
        content = []
        if full_text:
            content.append({"type": "text", "text": full_text})
        content.extend(tool_blocks)
        if not content:
            content.append({"type": "text", "text": ""})
        out = {
            "id": _new_msg_id(), "type": "message", "role": "assistant",
            "model": model_id, "content": content,
            "stop_reason": "tool_use" if tool_blocks else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }
        self._raw(200, "application/json", json.dumps(out).encode("utf-8"))

    # ---- low-level response helpers -------------------------------------
    def _relay_response(self, resp, streaming: bool):
        try:
            status = getattr(resp, "status", None) or resp.getcode()
            self.send_response(status)
            for k, v in resp.headers.items():
                kl = k.lower()
                if kl in _HOP_BY_HOP or kl in ("content-length", "content-encoding"):
                    continue
                self.send_header(k, v)
            if streaming:
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.close_connection = True
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception as e:
                    vlog("stream relay ended: %s" % e)
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _send_error(self, status: int, message: str):
        body = json.dumps({"type": "error",
                           "error": {"type": "proxy_error", "message": message}}).encode("utf-8")
        self._raw(status, "application/json", body)

    def _raw(self, status: int, ctype: str, payload: bytes):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            pass


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that caps simultaneous handler threads so a connection
    flood can't exhaust memory / file descriptors. Excess connections block in
    the accept loop until a slot frees up (back-pressure), rather than each
    spawning an unbounded thread. (#24)"""
    daemon_threads = True
    _slots = threading.BoundedSemaphore(MAX_CONNECTIONS) if MAX_CONNECTIONS > 0 else None

    def process_request(self, request, client_address):
        if self._slots is not None:
            self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            if self._slots is not None:
                self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            if self._slots is not None:
                self._slots.release()


def main():
    global UC_SLOT_MAP, UC_MODELS, LISTEN_PORT, UPSTREAM, MAX_TOKENS_FLOOR, ROUTER
    global INCLUDE_STOCK_MODELS, LEARN_STOCK_MODELS
    cfg_path = os.environ.get("UC_CONFIG", "") or _default_config_path()
    try:
        cfg = load_config(cfg_path)
        log("config: %s" % cfg_path)
    except FileNotFoundError:
        cfg = {}
        log("config not found (%s); copy config.example.json to config.json" % cfg_path)
    except Exception as e:
        cfg = {}
        log("config parse failed (%s): %s -- continuing with defaults" % (cfg_path, e))

    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    # Precedence: explicit env var > config.json > built-in default.
    if "UC_LISTEN_PORT" not in os.environ and proxy_cfg.get("listen_port"):
        LISTEN_PORT = int(proxy_cfg["listen_port"])
    if "UC_UPSTREAM" not in os.environ and proxy_cfg.get("anthropic_upstream"):
        UPSTREAM = str(proxy_cfg["anthropic_upstream"]).rstrip("/")
    if "UC_MAX_TOKENS" not in os.environ and proxy_cfg.get("max_tokens_floor"):
        MAX_TOKENS_FLOOR = int(proxy_cfg["max_tokens_floor"])
    # proxy.include_stock_models in config.json is honored unless the env var
    # was set explicitly (env always wins).
    if "UC_INCLUDE_STOCK_MODELS" not in os.environ and "include_stock_models" in proxy_cfg:
        INCLUDE_STOCK_MODELS = bool(proxy_cfg["include_stock_models"])
    if "UC_STOCK_LEARN" not in os.environ and "learn_stock_models" in proxy_cfg:
        LEARN_STOCK_MODELS = bool(proxy_cfg["learn_stock_models"])

    UC_SLOT_MAP = _routes_to_slots(cfg.get("routes"))
    UC_MODELS = _models_from_config(cfg.get("models"))
    _configure_router(cfg.get("router"))
    _wire_orchestrator_worker()
    _load_selection()   # restore a persisted orchestrator/worker pick (#18)
    _load_learned_stock()
    stock = _stock_models()
    if stock:
        with _LEARNED_STOCK_LOCK:
            n_learned = len(_LEARNED_STOCK)
        src = ("%d learned from upstream + baseline" % n_learned) if n_learned \
            else ("baseline; will learn from upstream"
                  if LEARN_STOCK_MODELS else "baseline (learning off)")
        log("  including %d stock Claude model(s) on GET /v1/models [%s] (real "
            "Claude stays visible): %s"
            % (len(stock), src, ", ".join(m["id"] for m in stock)))
    _configure_directives(cfg)
    if UC_MODELS:
        log("  advertising %d configured model(s) on GET /v1/models:" % len(UC_MODELS))
        for m in UC_MODELS:
            log("    %s  (%s)" % (m["id"], m["display_name"]))
    elif not stock:
        log("  no models configured (GET /v1/models passes through unchanged)")
    for mid, slot in UC_SLOT_MAP.items():
        log("  route %s -> type=%s model=%s upstream=%s"
            % (mid, slot.get("type", "anthropic"), slot.get("model", mid),
               slot.get("upstream", "(default)")))
    if _codex_oauth is None:
        log("  codex_oauth helper not importable (codex_oauth routes will 501)")
    if _cursor_agent is None:
        log("  cursor_agent helper not importable (cursor_agent routes will 501)")
    if _router_is_enabled():
        avail = _router_available_candidates()
        log("  Auto Router ON: id=%s classifier=%s threshold=%.2f candidates=%s"
            % (ROUTER["id"], ROUTER.get("classifier"), float(ROUTER.get("threshold", 0.7)),
               ", ".join("%s($%s)" % (c["id"], c.get("cost")) for c in avail) or "(none available)"))
        if not avail:
            log("  Auto Router WARNING: no candidate backend is configured; it will pass through")
        elif ROUTER.get("classifier") not in UC_SLOT_MAP:
            log("  Auto Router NOTE: classifier '%s' is not a configured route; "
                "using deterministic cheapest-candidate fallback" % ROUTER.get("classifier"))
    elif isinstance(cfg.get("router"), dict) and cfg["router"].get("enabled") and not ROUTER_ENABLED_ENV:
        log("  Auto Router disabled via UC_ROUTER=0")
    httpd = _BoundedThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log("ultracode-proxy listening on http://%s:%d -> %s"
        % (LISTEN_HOST, LISTEN_PORT, UPSTREAM))
    log("effort=%s thinking=%s max_tokens_floor=%d inject_reminder=%s"
        % (FORCE_EFFORT, FORCE_THINKING, MAX_TOKENS_FLOOR, INJECT_REMINDER))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
