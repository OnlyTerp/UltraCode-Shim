<#
.SYNOPSIS
  Launch Claude Code in UltraCode mode with the UltraCode-Shim model proxy, so
  the /model picker lists every backend you configured and any of them runs with
  full UltraCode behavior.

.DESCRIPTION
  Starts proxy.py on a loopback port, points Claude Code at it via
  ANTHROPIC_BASE_URL, enables gateway model discovery, seeds the discovery cache
  so your models show on first open, then runs `claude`. When Claude Code exits,
  the proxy is stopped.

  Your normal Claude Code install is untouched: this only sets environment for
  THIS process and uses a session-scoped --settings file.

.PARAMETER ProxyOnly
  Start the proxy and print how to connect, but don't launch Claude Code.

.PARAMETER Port
  Loopback port for the proxy. 0 (default) reads proxy.listen_port from
  config.json, falling back to 8141.

.EXAMPLE
  .\windows\Start-UltraCode.ps1
#>
param(
    [switch]$ProxyOnly,
    [int]$Port = 0,
    [string]$Upstream = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot      = Split-Path -Parent $PSScriptRoot
$Proxy         = Join-Path $RepoRoot "proxy.py"
$Config        = Join-Path $RepoRoot "config.json"
$ConfigExample = Join-Path $RepoRoot "config.example.json"
$EnvFile       = Join-Path $RepoRoot "ultracode.env"

# ----- locate Python --------------------------------------------------------
function Find-Python {
    foreach ($cand in @(@("py","-3"), @("python"), @("python3"))) {
        if (Get-Command $cand[0] -ErrorAction SilentlyContinue) { return ,$cand }
    }
    throw "Python 3 not found. Install it from https://www.python.org/downloads/ (check 'Add to PATH')."
}
$PyCmd = Find-Python

# ----- locate Claude Code ---------------------------------------------------
$Claude = (Get-Command claude -ErrorAction SilentlyContinue)
if (-not $Claude) {
    throw "Claude Code CLI not found. Install it with: npm i -g @anthropic-ai/claude-code"
}

# ----- ensure config.json exists (copy from the example on first run) -------
if (-not (Test-Path $Config)) {
    Copy-Item $ConfigExample $Config
    Write-Host "Created config.json from config.example.json - edit it to keep the models you have (and add your keys)." -ForegroundColor Yellow
}
$Cfg = Get-Content $Config -Raw | ConvertFrom-Json

# ----- resolve port + upstream from config.json (params/env override) -------
if ($Port -le 0) {
    $Port = 8141
    if ($Cfg.proxy.listen_port) { $Port = [int]$Cfg.proxy.listen_port }
}
if (-not $Upstream) {
    $Upstream = if ($Cfg.proxy.anthropic_upstream) { [string]$Cfg.proxy.anthropic_upstream } else { "https://api.anthropic.com" }
}

# ----- load optional ultracode.env into this process (for ${VAR} auth) ------
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $k, $v = $line.Split("=", 2)
            [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
        }
    }
}

# ----- state dir + session settings -----------------------------------------
$StateDir = Join-Path $env:LOCALAPPDATA "UltraCode-Shim"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$Settings = Join-Path $StateDir "ultracode_settings.json"
$BaseUrl  = "http://127.0.0.1:$Port"
@{
    ultracode = $true
    model     = "claude-opus-4-8"
    env       = @{
        ANTHROPIC_BASE_URL                         = $BaseUrl
        CLAUDE_CODE_WORKFLOWS                       = "1"
        CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY  = "1"
    }
} | ConvertTo-Json -Depth 5 | Set-Content -Path $Settings -Encoding utf8

# ----- proxy environment ----------------------------------------------------
$env:UC_CONFIG      = $Config
$env:UC_LISTEN_PORT = "$Port"
$env:UC_UPSTREAM    = $Upstream
$env:UC_LOG         = Join-Path $StateDir "ultracode_proxy.log"

# ----- shared-proxy lifecycle (reference-counted across sessions) -----------
# Several UltraCode sessions reuse one proxy on this port. We track its users
# with a marker file per session so one session exiting can't kill a proxy
# another is still using, and we stop it only when the LAST user exits. (The old
# behavior killed ALL proxy.py on start -- clobbering every other live session.)
$RefDir   = Join-Path $StateDir "refs"
$PidFile  = Join-Path $StateDir "proxy.pid"
$OwnerRef = Join-Path $RefDir "$PID"

function Test-ProxyHealthy {
    try { return (Invoke-WebRequest -Uri "$BaseUrl/healthz" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}
function Remove-DeadRefs {
    if (-not (Test-Path $RefDir)) { return }
    Get-ChildItem $RefDir -File -ErrorAction SilentlyContinue | ForEach-Object {
        $rpid = $_.Name
        if ($rpid -notmatch '^\d+$' -or -not (Get-Process -Id ([int]$rpid) -ErrorAction SilentlyContinue)) {
            Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue   # drop dead sessions' markers
        }
    }
}
function Test-RefsActive {
    Remove-DeadRefs
    return ((Test-Path $RefDir) -and @(Get-ChildItem $RefDir -File -ErrorAction SilentlyContinue).Count -gt 0)
}
function Stop-ProxyIfLast {
    Remove-Item $OwnerRef -Force -ErrorAction SilentlyContinue
    if (Test-RefsActive) { return }   # other live sessions still need it -> leave it running
    if (Test-Path $PidFile) {
        $stopId = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($stopId) { Stop-Process -Id ([int]$stopId) -Force -ErrorAction SilentlyContinue }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
}

New-Item -ItemType Directory -Force -Path $RefDir | Out-Null
New-Item -ItemType File -Force -Path $OwnerRef | Out-Null   # register this session as a proxy user

if (Test-ProxyHealthy) {
    Write-Host "Reusing the UltraCode proxy already running on $BaseUrl." -ForegroundColor Green
} else {
    Write-Host "Starting UltraCode proxy on $BaseUrl -> $Upstream ..."
    $pyArgs = @()
    if ($PyCmd.Count -gt 1) { $pyArgs += $PyCmd[1..($PyCmd.Count-1)] }
    $pyArgs += $Proxy
    $proc = Start-Process -FilePath $PyCmd[0] -ArgumentList $pyArgs -PassThru -WindowStyle Hidden
    Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii

    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-ProxyHealthy) { $ready = $true; break }
    }
    if (-not $ready) {
        Write-Error "Proxy did not become healthy on port $Port. Log: $($env:UC_LOG)"
        if (Test-Path $env:UC_LOG) { Get-Content $env:UC_LOG -Tail 20 }
        Remove-Item $OwnerRef -Force -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "Proxy healthy (pid $($proc.Id))." -ForegroundColor Green
}

# ----- seed Claude Code's gateway-models cache (first-launch visibility) -----
$CfgDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME ".claude" }
$GwCache = Join-Path (Join-Path $CfgDir "cache") "gateway-models.json"
try {
    New-Item -ItemType Directory -Force -Path (Split-Path $GwCache) | Out-Null
    $seed = [ordered]@{
        baseUrl   = $BaseUrl
        fetchedAt = [int64]([datetimeoffset](Get-Date)).ToUnixTimeMilliseconds()
        models    = @($Cfg.models | ForEach-Object { [ordered]@{ id = $_.id; display_name = $_.display_name } })
    }
    $seed | ConvertTo-Json -Depth 5 | Set-Content -Path $GwCache -Encoding utf8
    Write-Host "Seeded gateway-models cache ($(@($Cfg.models).Count) models)."
} catch {
    Write-Host "WARN: could not seed gateway-models cache: $_" -ForegroundColor Yellow
}

if ($ProxyOnly) {
    # Leave the proxy up for manual use; this transient launcher is exiting now,
    # so drop its ref rather than have it pin/stop the proxy.
    Remove-Item $OwnerRef -Force -ErrorAction SilentlyContinue
    $shownPid = if (Test-Path $PidFile) { Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1 } else { "<pid>" }
    Write-Host ""
    Write-Host "Proxy running. Connect Claude Code with:"
    Write-Host "  `$env:ANTHROPIC_BASE_URL='$BaseUrl'"
    Write-Host "  claude --settings `"$Settings`""
    Write-Host "Stop the proxy: Stop-Process -Id $shownPid"
    exit 0
}

# ----- launch Claude Code through the proxy ---------------------------------
$env:ANTHROPIC_BASE_URL = $BaseUrl
$env:CLAUDE_CODE_WORKFLOWS = "1"
$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1"
Write-Host "Launching Claude Code (UltraCode). Open /model to pick a backend." -ForegroundColor Green
try {
    & $Claude.Source --settings "$Settings" @args
} finally {
    Write-Host "Claude exited. Releasing the UltraCode proxy."
    Stop-ProxyIfLast
}
