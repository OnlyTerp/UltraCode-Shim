<p align="center">
  <img src="../assets/brand/doc-setup.png" alt="Setup — get running on Windows 11 or macOS / Linux / WSL" width="100%">
</p>

Works on **Windows 11** (no WSL required) and on **macOS / Linux / WSL**.

## 1. Prerequisites

| Need | Check | Get it |
|------|-------|--------|
| Python 3.8+ | `python --version` / `python3 --version` | https://www.python.org/downloads (Windows: tick **Add Python to PATH**) |
| Claude Code CLI | `claude --version` | `npm i -g @anthropic-ai/claude-code` |
| UltraCode access | you've used `/effort ultracode` before | part of your Claude plan |
| ≥1 backend credential | — | an API key and/or a login (`codex login` / `grok login --oauth`) (see below) |

There is **nothing to pip install** — the proxy is pure standard library.

## 2. Install (one command)

The installer gets the code, runs the offline self-test, creates your
`config.json`, and drops a `ultracode` launcher on your PATH.

**macOS / Linux / WSL**

```bash
curl -fsSL https://raw.githubusercontent.com/OnlyTerp/UltraCode-Shim/main/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/OnlyTerp/UltraCode-Shim/main/install.ps1 | iex
```

Already have a clone? Run `./install.sh` (or `.\install.ps1`) from inside it — it
detects the checkout and skips the network clone. Useful flags:

- `--no-test` / `-NoTest` — skip the offline self-test.
- `--dir DIR` / `-Dir DIR` — where to clone (default `~/.ultracode-shim`, or
  `%LOCALAPPDATA%\UltraCode-Shim` on Windows).
- `--bin-dir DIR` / `-BinDir DIR` — where to put the `ultracode` command.
- `-DesktopIcons` (Windows) — also create the Desktop shortcuts.
- `--uninstall` / `-Uninstall` — remove the launcher (leaves your clone + config).

If the installer says your bin dir isn't on `PATH`, it prints the exact line to
add. Re-open your terminal afterward.

> Prefer to do it by hand? See **[Manual setup](#manual-setup-no-installer)** at
> the bottom — clone, `doctor.py`, then `bin/ultracode`.

## 3. Configure your models

The installer (and the launcher, on first run) creates `config.json` from
`config.example.json`. `config.json` is gitignored, so your keys never get
committed. Edit it for the plans you have: keep the entries you want in `models` +
`routes`, delete the rest, and put each key inline or as `${VAR}`. Full
per-backend templates are in [ADD_A_MODEL.md](ADD_A_MODEL.md).

> **Real Claude (Opus/Sonnet/Haiku) is always offered in `/model`** — you don't
> configure it, and it stays in the picker even with no Anthropic key to list it.
> The list is **self-updating**: the proxy learns the real Claude ids from any
> successful upstream `/v1/models` fetch and caches them, so a newly released Opus
> appears automatically. Turn it off with `proxy.include_stock_models: false` (or
> `UC_INCLUDE_STOCK_MODELS=0`); disable just the learning with
> `proxy.learn_stock_models: false` (or `UC_STOCK_LEARN=0`).

### GPT‑5.5 via ChatGPT/Codex login (optional)

If you want the `codex_oauth` backend:

1. Install the Codex CLI and run `codex login` once. This creates `~/.codex/auth.json`.
2. Keep the `claude-gpt-5.5-codex` entries in the example configs (or add your own).

No API key is needed for this path — it reuses your ChatGPT login.

### Grok via a Grok subscription login (optional)

If you want the `grok_build` backend (uses your SuperGrok / X Premium+ plan, not
a metered xAI API key):

1. Install the official Grok CLI and run `grok login --oauth` once — or
   `grok login --device-auth` on a headless box. This creates `~/.grok/auth.json`.
2. Keep the `claude-grok-4-6` entries in the example configs (or add your own,
   with `"type": "grok_build"` and a real xAI `model` like `grok-4.6`).

No API key is needed — the proxy reads that login token and refreshes it for you.

## 4. Sanity-check the config (optional)

The installer already ran the offline self-test. After you edit `config.json` you
can re-validate your real config anytime:

```
python scripts/doctor.py        # windows: python   |  mac/linux: python3
```

Resolve any `[FAIL]` lines (each prints the fix) until it exits cleanly. (If you
installed via the one-command flow, the repo lives in `~/.ultracode-shim` or
`%LOCALAPPDATA%\UltraCode-Shim` unless you passed `--dir`.)

## 5. Use it

```
ultracode
```

First the selector opens: pick an orchestrator on the left and a worker on the
right (`Same as orchestrator` means one model runs everything). Claude Code then
opens with full UltraCode. You can still type `/model` later to change either tier
— your configured models **and** real Claude (Opus/Sonnet/Haiku) are all there.

Set `UC_SELECTOR=0` before launching to skip the selector and choose from
`/model` only.

> **Windows Desktop icons (optional).** Prefer double-clicking? Run
> `.\install.ps1 -DesktopIcons` (or, in a clone,
> `.\windows\Install-DesktopIcons.ps1`) to create **UltraCode (All Models)** and
> **Claude Code (Normal)** shortcuts. If PowerShell blocks the script, run it once
> as `powershell -ExecutionPolicy Bypass -File .\windows\Install-DesktopIcons.ps1`.

## Manual setup (no installer)

```
git clone https://github.com/OnlyTerp/UltraCode-Shim.git
cd UltraCode-Shim
python scripts/doctor.py                 # offline self-test + (later) config check
cp config.example.json config.json       # windows: copy ...   then edit it
./bin/ultracode                          # windows: .\windows\Start-UltraCode.ps1
```

Optionally symlink the launcher onto your PATH:
`ln -s "$PWD/bin/ultracode" ~/.local/bin/ultracode`.

## Uninstall

- Launcher command: `./install.sh --uninstall` (or `.\install.ps1 -Uninstall`)
  removes the `ultracode` shim; your clone and `config.json` are left alone.
- Windows icons + session state: `.\windows\Uninstall.ps1`.
- Everywhere: delete the repo folder. Your `~/.claude` and credentials are never
  modified by this project.
