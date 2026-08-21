#!/usr/bin/env python3
"""
grok_build.py -- optional helper that lets UltraCode-Shim route a model to
xAI Grok using a Grok *login* (your SuperGrok / X subscription) instead of a
metered xAI API key.

This is only used by routes whose "type" is "grok_build". It is pure Python
standard library (no pip install). It reuses the credentials created by the
official Grok CLI, so the user must first run:

    grok login --oauth          (or, headless: grok login --device-auth)

WHAT IT IS NOT
--------------
It is NOT a full backend adapter. Grok's API is plain OpenAI Chat Completions
at https://api.x.ai/v1, which proxy.py already speaks. So this module ONLY
resolves a fresh OAuth access token -- refreshing it against auth.x.ai when it
is near expiry. proxy.py's _handle_grok() injects that token as the Bearer for
the normal openai_compat path, so all message / tool-call / streaming / reasoning
translation is reused (DRY), never duplicated here.

The Grok CLI stores its OAuth state in ~/.grok/auth.json, keyed by
"<issuer>::<client_id>", e.g.:

    {
      "https://auth.x.ai::<client_id>": {
        "key": "<access_token>",
        "refresh_token": "<refresh_token>",
        "expires_at": "2026-08-21T13:36:32.429328Z",
        "oidc_issuer": "https://auth.x.ai",
        "oidc_client_id": "<client_id>",
        "auth_mode": "oidc", ...
      }
    }

ENV KNOBS
---------
  GROK_HOME             dir holding auth.json   (default ~/.grok)
  UC_GROK_TOKEN_URL     OAuth token endpoint    (default https://auth.x.ai/oauth2/token)
  UC_GROK_REFRESH_SKEW  refresh this many seconds before expiry (default 120)
"""

import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path


class GrokAuthError(Exception):
    pass


# --------------------------------------------------------------------------
# Paths / config (resolved dynamically so tests can point GROK_HOME at a tmp dir)
# --------------------------------------------------------------------------

def _auth_file() -> Path:
    return Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))) / "auth.json"


def _token_url() -> str:
    return os.environ.get("UC_GROK_TOKEN_URL", "https://auth.x.ai/oauth2/token")


def _refresh_skew() -> int:
    try:
        return int(os.environ.get("UC_GROK_REFRESH_SKEW", "120"))
    except ValueError:
        return 120


def available() -> bool:
    return _auth_file().is_file()


# --------------------------------------------------------------------------
# Auth
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


def _pick_entry(state: dict):
    # auth.json maps "<issuer>::<client_id>" -> credential dict. Pick the first
    # OIDC entry that carries an access token ("key"). Returns (top_key, entry).
    for k, v in (state or {}).items():
        if isinstance(v, dict) and v.get("key"):
            return k, v
    raise GrokAuthError(
        "no Grok OAuth token in %s -- run `grok login --oauth`." % _auth_file())


def _expires_epoch(entry: dict) -> float:
    ts = entry.get("expires_at")
    if not ts:
        return 0.0
    try:
        # ISO 8601, trailing Z, microseconds optional.
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _is_expiring(entry: dict, skew=None) -> bool:
    skew = _refresh_skew() if skew is None else skew
    exp = _expires_epoch(entry)
    # Unknown expiry -> assume usable and let a real 401 surface, rather than
    # forcing a refresh we might not need.
    if not exp:
        return False
    return time.time() >= (exp - skew)


def _refresh(top_key: str, entry: dict) -> dict:
    refresh_token = entry.get("refresh_token")
    client_id = entry.get("oidc_client_id")
    if not refresh_token or not client_id:
        raise GrokAuthError(
            "cannot refresh Grok token (missing refresh_token/oidc_client_id) -- "
            "run `grok login --oauth`.")
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode("utf-8")
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
        raise GrokAuthError("Grok token refresh HTTP %s: %s -- try `grok login --oauth`."
                            % (e.code, detail))
    except Exception as e:
        raise GrokAuthError("Grok token refresh failed: %s" % e)
    access = tok.get("access_token")
    if not access:
        raise GrokAuthError("Grok token refresh returned no access_token.")
    # Merge refreshed fields back and persist so the next call reuses them.
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
    _persist(top_key, entry)
    return entry


def _persist(top_key: str, entry: dict) -> None:
    # Best-effort write-back. If anything races or fails we still return the
    # in-memory token; the Grok CLI ultimately owns the file.
    try:
        state = _load_auth()
    except Exception:
        state = {}
    state[top_key] = entry
    try:
        f = _auth_file()
        tmp = f.with_name(f.name + ".uctmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(f))
        try:
            os.chmod(str(f), 0o600)
        except Exception:
            pass
    except Exception:
        pass


def access_token() -> str:
    """Return a currently-valid Grok OAuth access token, refreshing if needed."""
    state = _load_auth()
    top_key, entry = _pick_entry(state)
    if _is_expiring(entry):
        entry = _refresh(top_key, entry)
    token = entry.get("key")
    if not token:
        raise GrokAuthError(
            "no Grok access token in %s -- run `grok login --oauth`." % _auth_file())
    return token
