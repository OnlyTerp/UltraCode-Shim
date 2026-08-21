#!/usr/bin/env python3
"""
grok_build.py -- optional helper that lets UltraCode-Shim route a model to
xAI Grok using a Grok *login* (your SuperGrok / X Premium+ subscription) instead
of a metered xAI API key.

Only used by routes whose "type" is "grok_build". Pure Python standard library
(no pip install). It reuses the credentials the official Grok CLI writes, so the
user must first run:

    grok login --oauth          (or, headless: grok login --device-auth)

WHAT IT IS / IS NOT
-------------------
Not a full backend adapter. The subscription inference endpoint speaks plain
OpenAI Chat Completions, which proxy.py already handles. So this module only:
  * selects the right OIDC credential from ~/.grok/auth.json,
  * refreshes the OAuth token against auth.x.ai when it nears expiry (serialized
    across handler threads so the rotating refresh token is never double-spent),
  * supplies the pinned CLI-proxy endpoint + the session headers it requires.
proxy.py's _handle_grok() then reuses the openai_compat path with that endpoint,
bearer, and headers.

WHY THE CLI PROXY (not api.x.ai)
--------------------------------
The official Grok CLI does session inference against
https://cli-chat-proxy.grok.com/v1 (its default); xAI's own endpoint resolver
comments that api.x.ai "is the inference endpoint (API-key auth) only." A
subscription token also works against api.x.ai, but that is the API-key surface,
not the sanctioned session path, so the endpoint is PINNED to the CLI proxy. A
route-supplied `upstream` is ignored (sending the implicitly-loaded OAuth bearer
to an arbitrary host would leak it). (Whether api.x.ai bills a subscription token
differently is unverified -- the pin follows xAI's client design, not a measured
billing difference.)

ENV KNOBS
---------
  GROK_HOME               dir holding auth.json / version.json (default ~/.grok)
  UC_GROK_BASE_URL        inference base URL  (default https://cli-chat-proxy.grok.com/v1)
  UC_GROK_TOKEN_URL       OAuth token endpoint (default https://auth.x.ai/oauth2/token)
  UC_GROK_REFRESH_SKEW    refresh this many seconds before expiry (default 120)
  UC_GROK_CLIENT_VERSION  override the CLI version header (else read version.json)
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path


class GrokAuthError(Exception):
    pass


# Pinned CLI-proxy contract (see module docstring).
DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_TOKEN_URL = "https://auth.x.ai/oauth2/token"
TOKEN_AUTH_VALUE = "xai-grok-cli"
# 30-day fallback matches the official CLI when the server omits an expiry.
MISSING_EXPIRY_TTL = 30 * 24 * 3600

# Serialize refresh across handler threads (the server is threaded) so a rotating
# refresh token is never double-spent.
_LOCK = threading.Lock()
# Freshest-known credential per top-level auth.json key. Survives a failed disk
# write, so a rotated refresh token is never lost (a re-read from disk would
# otherwise hand back the consumed token).
_CACHE = {}


# --------------------------------------------------------------------------
# Paths / config (resolved dynamically so tests can point GROK_HOME at a tmp dir)
# --------------------------------------------------------------------------

def _grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))


def _auth_file() -> Path:
    return _grok_home() / "auth.json"


def base_url() -> str:
    return os.environ.get("UC_GROK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _token_url() -> str:
    return os.environ.get("UC_GROK_TOKEN_URL", DEFAULT_TOKEN_URL)


def _refresh_skew() -> int:
    try:
        return int(os.environ.get("UC_GROK_REFRESH_SKEW", "120"))
    except ValueError:
        return 120


def client_version() -> str:
    """The installed Grok CLI's semantic version, for the x-grok-client-version
    gate the CLI proxy enforces. Prefer an explicit override, then version.json.
    Hard-fail if neither is available (do not fabricate a version)."""
    ov = os.environ.get("UC_GROK_CLIENT_VERSION", "").strip()
    if ov:
        return ov
    vf = _grok_home() / "version.json"
    if vf.is_file():
        try:
            v = (json.loads(vf.read_text(encoding="utf-8")).get("version") or "").strip()
            if re.match(r"^\d+\.\d+\.\d+", v):
                return v
        except Exception:
            pass
    raise GrokAuthError(
        "could not determine the Grok CLI version (%s) for the "
        "x-grok-client-version header -- reinstall/`grok update`, or set "
        "UC_GROK_CLIENT_VERSION." % (_grok_home() / "version.json"))


# --------------------------------------------------------------------------
# auth.json read / credential selection
# --------------------------------------------------------------------------

def _load_auth() -> dict:
    f = _auth_file()
    if not f.is_file():
        raise GrokAuthError(
            "no %s -- run `grok login --oauth` first (install the Grok CLI if needed)." % f)
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        raise GrokAuthError("could not read %s: %s" % (f, e))


def _is_oidc(top_key, entry) -> bool:
    # An eligible subscription credential: OIDC mode with both an access token
    # ("key") and a refresh token. Never an xai::api_key entry or a legacy session.
    if not isinstance(entry, dict) or not entry.get("key") or not entry.get("refresh_token"):
        return False
    return entry.get("auth_mode") == "oidc" or str(top_key).startswith("https://auth.x.ai::")


def _pick_entry(state: dict):
    """Return (top_key, entry) for the newest eligible OIDC credential."""
    eligible = [(k, v) for k, v in (state or {}).items() if _is_oidc(k, v)]
    if not eligible:
        raise GrokAuthError(
            "no Grok subscription (OIDC) login in %s -- run `grok login --oauth` "
            "(an xAI API key alone is not a grok_build credential)." % _auth_file())

    # Newest by expiry (then create_time), so a fresh login wins over a stale one.
    def _newest(kv):
        _, e = kv
        _, ep = _expiry_state(e)
        return ep or _parse_iso(e.get("create_time")) or 0.0
    eligible.sort(key=_newest, reverse=True)
    return eligible[0]


# --------------------------------------------------------------------------
# Expiry policy
# --------------------------------------------------------------------------

def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _expiry_state(entry: dict):
    """Return (state, epoch). state in {'ok','malformed','missing_no_ctime'}."""
    ts = entry.get("expires_at")
    if ts is None or ts == "":
        base = _parse_iso(entry.get("create_time"))
        if base is None:
            return ("missing_no_ctime", None)      # cannot bound -> force refresh
        return ("ok", base + MISSING_EXPIRY_TTL)    # 30-day fallback like the CLI
    ep = _parse_iso(ts)
    if ep is None:
        return ("malformed", None)                  # present but junk -> force refresh
    return ("ok", ep)


def _needs_refresh(entry: dict, skew=None) -> bool:
    skew = _refresh_skew() if skew is None else skew
    state, ep = _expiry_state(entry)
    if state != "ok" or ep is None:
        return True                                 # unknown expiry -> refresh
    return time.time() >= (ep - skew)


# --------------------------------------------------------------------------
# Refresh + safe persist
# --------------------------------------------------------------------------

def _refresh(top_key: str, entry: dict) -> dict:
    refresh_token = entry.get("refresh_token")
    client_id = entry.get("oidc_client_id")
    if not refresh_token or not client_id:
        raise GrokAuthError(
            "cannot refresh Grok token (missing refresh_token/oidc_client_id) -- "
            "run `grok login --oauth`.")
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id}
    # Forward principal fields when present, matching the official CLI (a
    # team-scoped credential can otherwise fail or mint a personal token).
    for f in ("principal_type", "principal_id"):
        if entry.get(f):
            form[f] = entry[f]
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        _token_url(), data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        tok = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise GrokAuthError("Grok token refresh HTTP %s: %s -- run `grok login --oauth`."
                            % (e.code, detail))
    except Exception as e:
        raise GrokAuthError("Grok token refresh failed: %s" % e)
    access = tok.get("access_token")
    if not access:
        raise GrokAuthError("Grok token refresh returned no access_token.")
    entry = dict(entry)
    entry["key"] = access
    if tok.get("refresh_token"):
        entry["refresh_token"] = tok["refresh_token"]
    if tok.get("expires_in"):
        try:
            newexp = datetime.now(timezone.utc) + timedelta(seconds=int(tok["expires_in"]))
            entry["expires_at"] = newexp.isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    _CACHE[top_key] = entry          # cache BEFORE persist so a failed write can't lose it
    _persist(top_key, entry)
    return entry


def _persist(top_key: str, entry: dict) -> None:
    """Write the refreshed credential back. Best-effort, but NEVER destructive:
    a refresh always started from an existing file, so if that file cannot be
    re-read we skip the write rather than replace the whole store (which would
    wipe other scopes, e.g. an api_key entry or a second account)."""
    f = _auth_file()
    if f.exists():
        try:
            state = _load_auth()
        except Exception:
            return                    # unreadable existing file -> do not clobber it
    else:
        state = {}
    state[top_key] = entry
    try:
        tmp = f.with_name(f.name + ".uctmp")
        # Create the temp at 0600 up front so tokens are never briefly world-readable.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(state, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(str(tmp), str(f))
    except Exception:
        pass


def _current_entry():
    """Freshest known (top_key, entry): prefer a still-valid cached credential
    over disk so a token rotated after a failed persist is not lost."""
    top_key, disk = _pick_entry(_load_auth())
    cached = _CACHE.get(top_key)
    if cached and not _needs_refresh(cached):
        return top_key, cached
    return top_key, disk


def access_token() -> str:
    """Return a currently-valid Grok OAuth access token, refreshing if needed.
    Serialized so concurrent handler threads never double-spend the refresh token."""
    with _LOCK:
        top_key, entry = _current_entry()
        if _needs_refresh(entry):
            entry = _refresh(top_key, entry)
        _CACHE[top_key] = entry
        token = entry.get("key")
        if not token:
            raise GrokAuthError(
                "no Grok access token in %s -- run `grok login --oauth`." % _auth_file())
        return token


def request_headers(model: str) -> dict:
    """The session headers the CLI proxy requires (Authorization is added by the
    openai_compat path from the route auth)."""
    h = {
        "X-XAI-Token-Auth": TOKEN_AUTH_VALUE,
        "x-authenticateresponse": "authenticate-response",
        "x-grok-client-version": client_version(),
        "x-grok-client-mode": "headless",
    }
    if model:
        h["x-grok-model-override"] = model
    return h


def available() -> bool:
    """True only when a usable subscription (OIDC) credential is present -- used
    by the doctor so a bare/empty/api-key-only auth.json is not a false 'ok'."""
    try:
        _pick_entry(_load_auth())
        return True
    except Exception:
        return False
