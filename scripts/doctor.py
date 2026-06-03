#!/usr/bin/env python3
"""
UltraCode-Shim doctor: verify a machine is ready, validate your config, and run
the offline self-test. Cross-platform, standard library only.

    python3 scripts/doctor.py            # full check
    python3 scripts/doctor.py --no-test  # skip the proxy self-test

Exit code is non-zero if anything REQUIRED is missing, so an AI or CI can gate
on it. Each failure prints the one command that fixes it.
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROXY = REPO / "proxy.py"
TEST = REPO / "test_proxy.py"
CONFIG = REPO / "config.json"
CONFIG_EXAMPLE = REPO / "config.example.json"
ENV_FILE = REPO / "ultracode.env"

OK = "[ok]  "
NOTE = "[note]"
FAIL = "[FAIL]"
counts = {"ok": 0, "note": 0, "fail": 0}


def ok(m): counts.__setitem__("ok", counts["ok"] + 1); print(OK, m, flush=True)
def note(m): counts.__setitem__("note", counts["note"] + 1); print(NOTE, m, flush=True)
def fail(m): counts.__setitem__("fail", counts["fail"] + 1); print(FAIL, m, flush=True)


def load_env_file(p: Path):
    """Optionally load an `ultracode.env` (gitignored) so ${VAR} refs resolve."""
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def strip_comments(obj):
    """Drop keys starting with '_' (inline documentation in config.json)."""
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [strip_comments(x) for x in obj]
    return obj


def referenced_vars(s):
    return re.findall(r"\$\{([^}]+)\}", s or "") if isinstance(s, str) else []


def looks_like_placeholder(s):
    return isinstance(s, str) and bool(re.search(r"REPLACE_WITH|your-|YOUR_", s))


def proxy_cfg_get(cfg, key, default=None):
    """Read a key from the config's 'proxy' block, tolerating a missing block."""
    proxy = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    return proxy.get(key, default)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-test", action="store_true", help="skip the proxy self-test")
    ap.add_argument("--ci", action="store_true",
                    help="CI mode: don't fail on a missing Claude Code CLI")
    args = ap.parse_args()

    print("UltraCode-Shim doctor")
    print("repo:", REPO)
    print()

    # 1. Python
    if sys.version_info >= (3, 8):
        ok("python %d.%d.%d" % sys.version_info[:3])
    else:
        fail("python >= 3.8 required; found %d.%d" % sys.version_info[:2])

    # 2. proxy present + compiles
    if PROXY.is_file():
        rc = subprocess.run([sys.executable, "-m", "py_compile", str(PROXY)]).returncode
        ok("proxy.py present and compiles") if rc == 0 \
            else fail("proxy.py has a syntax error")
    else:
        fail("missing proxy.py")

    # 3. claude CLI
    if shutil.which("claude"):
        ok("claude CLI on PATH: %s" % shutil.which("claude"))
    elif args.ci:
        note("claude CLI not found (CI mode - skipping)")
    else:
        fail("claude CLI not found - install: npm i -g @anthropic-ai/claude-code")

    # 4. config: load optional env, pick config.json (fall back to the example)
    load_env_file(ENV_FILE)
    cfg_path = CONFIG
    using_example = False
    if not cfg_path.is_file():
        cfg_path = CONFIG_EXAMPLE
        using_example = True
        note("no config.json yet - validating config.example.json "
             "(the launcher copies it to config.json on first run)")

    cfg = {}
    try:
        cfg = strip_comments(json.loads(cfg_path.read_text(encoding="utf-8")))
        ok("config parses: %s" % cfg_path.name)
    except Exception as e:
        fail("could not parse %s: %s" % (cfg_path.name, e))

    models = cfg.get("models") if isinstance(cfg.get("models"), list) else []
    routes = cfg.get("routes") if isinstance(cfg.get("routes"), dict) else {}
    ok("config has %d model(s) and %d route(s)" % (len(models), len(routes)))

    # 5. discovery rule: ids must start with claude/anthropic and be routed
    model_ids = [m.get("id") for m in models if isinstance(m, dict)]
    discovery_fails = 0
    for mid in model_ids:
        if not re.match(r"^(claude|anthropic)", mid or "", re.I):
            fail("model id '%s' will NOT appear in /model (must start with 'claude' or 'anthropic')" % mid)
            discovery_fails += 1
        if mid not in routes:
            fail("model '%s' has no entry in routes - add a matching route" % mid)
            discovery_fails += 1
    if model_ids and discovery_fails == 0:
        ok("all advertised model ids are discoverable and routed")

    # 5.5 stock Claude models (real Claude stays in /model even with no upstream key)
    include_stock = os.environ.get("UC_INCLUDE_STOCK_MODELS",
                                   "0" if proxy_cfg_get(cfg, "include_stock_models") is False else "1") != "0"
    learn_stock = os.environ.get("UC_STOCK_LEARN",
                                 "0" if proxy_cfg_get(cfg, "learn_stock_models") is False else "1") != "0"
    if include_stock:
        ok("stock Claude models (Opus/Sonnet/Haiku) will also appear in /model "
           "(real Claude never drops out; disable with UC_INCLUDE_STOCK_MODELS=0)")
        if learn_stock:
            ok("stock-model auto-learning is ON - the proxy learns new Claude ids "
               "from upstream /v1/models, so a future Opus shows up automatically "
               "(disable with UC_STOCK_LEARN=0)")
        else:
            note("stock-model auto-learning is OFF (UC_STOCK_LEARN=0 / "
                 "proxy.learn_stock_models=false) - using only the built-in baseline list")
    else:
        note("stock Claude models are disabled (UC_INCLUDE_STOCK_MODELS=0 / "
             "proxy.include_stock_models=false) - only your configured models will show")

    # 6. per-route backend checks
    for name, route in routes.items():
        if not isinstance(route, dict):
            continue
        rtype = route.get("type", "anthropic")
        if rtype == "codex_oauth":
            auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
            ok("route '%s': Codex login found (%s)" % (name, auth)) if auth.is_file() \
                else note("route '%s': no %s - run `codex login` before using it" % (name, auth))
            continue
        if rtype == "cursor_agent":
            binp = (os.environ.get("CURSOR_AGENT_BIN") or shutil.which("cursor-agent")
                    or str(Path.home() / ".local" / "bin" / "cursor-agent"))
            ok("route '%s': cursor-agent found (%s)" % (name, binp)) if (binp and Path(binp).exists()) \
                else note("route '%s': cursor-agent not found - install it and run "
                          "`cursor-agent login` (this backend is experimental)" % name)
            continue
        # anthropic passthrough or openai_compat: validate the credential.
        auth = route.get("auth", "")
        refs = set(referenced_vars(auth))
        for hv in (route.get("headers") or {}).values():
            refs.update(referenced_vars(hv))
        for var in sorted(refs):
            if os.environ.get(var):
                ok("route '%s': env var %s is set" % (name, var))
            elif using_example:
                note("route '%s': %s not set yet (example backend; set it once you keep this route)" % (name, var))
            else:
                fail("route '%s': %s is empty - export it or put the key inline in config.json" % (name, var))
        if not refs and looks_like_placeholder(auth):
            note("route '%s': auth still has a placeholder (%s) - put your real key there"
                 % (name, auth)) if using_example else \
                fail("route '%s': auth still has a placeholder - replace it with your real key" % name)

    # 6.5 Auto Router validation (optional feature)
    router = cfg.get("router") if isinstance(cfg.get("router"), dict) else {}
    if router and router.get("enabled"):
        rid = router.get("id") or "claude-auto"
        rslot = routes.get(rid) if isinstance(routes.get(rid), dict) else None
        if rslot and rslot.get("type") == "auto" and rid in model_ids:
            ok("router: picker '%s' is a model + type:auto route" % rid)
        else:
            (note if using_example else fail)(
                "router: '%s' must appear in 'models' and have a {\"type\":\"auto\"} route" % rid)

        thr = router.get("threshold", 0.7)
        if isinstance(thr, (int, float)) and 0 < float(thr) <= 1:
            ok("router: threshold %.2f" % float(thr))
        else:
            fail("router: threshold must be a number in (0, 1]; got %r" % thr)

        cands = router.get("candidates") if isinstance(router.get("candidates"), list) else []
        avail = 0
        for c in cands:
            if not isinstance(c, dict) or not c.get("id"):
                fail("router: each candidate needs an 'id'")
                continue
            cid = c["id"]
            if cid not in routes:
                (note if using_example else fail)(
                    "router: candidate '%s' has no matching route (it will be skipped)" % cid)
            else:
                avail += 1
            if "cost" in c and not isinstance(c["cost"], (int, float)):
                fail("router: candidate '%s' cost must be a number" % cid)
        if avail >= 1:
            ok("router: %d candidate backend(s) available" % avail)
        else:
            (note if using_example else fail)(
                "router: no candidate has a configured route - add at least one")

        clf = router.get("classifier")
        if clf and clf in routes:
            ok("router: classifier '%s' is configured" % clf)
        elif clf:
            note("router: classifier '%s' not configured - router will fall back to the "
                 "cheapest candidate deterministically (no scoring)" % clf)
        else:
            note("router: no classifier set - router will pick the cheapest candidate without scoring")

    # 6b. routing directives (pins): validate alias overrides + planner when set
    directives = cfg.get("directives") if isinstance(cfg.get("directives"), dict) else {}
    if directives:
        if directives.get("enabled"):
            al = directives.get("aliases") if isinstance(directives.get("aliases"), dict) else {}
            bad = [v for v in al.values() if not (isinstance(v, str) and v in routes)]
            if bad:
                (note if using_example else fail)(
                    "directives: alias override(s) point to missing route(s): %s" % ", ".join(map(str, bad)))
            elif al:
                ok("directives: %d alias override(s) map to real routes" % len(al))
            planner = directives.get("planner")
            if not planner:
                ok("directives: enabled (no planner)")
            elif planner in routes:
                ok("directives: planner '%s' is a configured route" % planner)
            else:
                (note if using_example else fail)(
                    "directives: planner '%s' has no matching route - it will be ignored" % planner)
        else:
            ok("directives: present but disabled (no-op)")

    # 7. port free
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    port = int(os.environ.get("UC_LISTEN_PORT") or proxy_cfg.get("listen_port") or 8141)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free = s.connect_ex(("127.0.0.1", port)) != 0
    s.close()
    ok("port %d is free" % port) if free else \
        note("port %d already in use - a proxy may already be running (fine), or pick another" % port)

    # 8. offline self-test
    if not args.no_test and TEST.is_file():
        print("\nrunning offline self-test (test_proxy.py)...", flush=True)
        rc = subprocess.run([sys.executable, str(TEST)]).returncode
        ok("self-test passed") if rc == 0 else fail("self-test failed (see output above)")

    print()
    print("Result: %d ok, %d notes, %d failures" % (counts["ok"], counts["note"], counts["fail"]))
    if counts["fail"]:
        print("Fix the [FAIL] lines above, then re-run: python3 scripts/doctor.py")
        return 1
    if using_example:
        print("Looks good. Run the installer to set up config.json + the `ultracode` "
              "command:\n  mac/linux/WSL:  ./install.sh\n  windows:        .\\install.ps1")
    else:
        print("Ready. Launch with:  ultracode   (after ./install.sh or .\\install.ps1)")
        print("  or directly:       bin/ultracode  (mac/linux/WSL)  |  "
              "windows\\Start-UltraCode.ps1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
