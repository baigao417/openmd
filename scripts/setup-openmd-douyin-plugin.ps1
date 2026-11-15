[CmdletBinding()]
param(
    [switch] $Install
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$pluginDir = Join-Path $repoRoot "integrations\opencli\openmd-douyin"
$manifestPath = Join-Path $pluginDir "opencli-plugin.json"
$sourcePath = Join-Path $pluginDir "creator-videos.ts"
$runtimePath = Join-Path $pluginDir "creator-videos.js"

# Prefer Openmd's private managed host when the user has no standalone host.
# This only changes this PowerShell process; it never edits ~/.opencli or the
# machine-wide PATH.
$dataRoot = if ($env:OPENMD_DATA_DIR) { $env:OPENMD_DATA_DIR } else { Join-Path $HOME ".openmd" }
$managedBin = Join-Path $dataRoot "tools\opencli\node_modules\.bin"
if (Test-Path -LiteralPath $managedBin -PathType Container) {
    $env:PATH = "$managedBin$([IO.Path]::PathSeparator)$env:PATH"
}
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    $activationCode = "import os,sys;sys.path.insert(0, r'$repoRoot\src');from openmd.runtime.managed_tools import activate;activate();print(os.environ.get('PATH',''))"
    $activatedPath = & $python.Source -c $activationCode 2>$null | Select-Object -Last 1
    if ($LASTEXITCODE -eq 0 -and $activatedPath) { $env:PATH = [string]$activatedPath }
}

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Openmd Douyin plugin manifest not found: $manifestPath"
}

if (-not (Get-Command opencli -ErrorAction SilentlyContinue)) {
    throw "opencli is not available on PATH. Install or repair OpenCLI before registering the Openmd Douyin plugin."
}

if ($Install) {
    if (-not (Get-Command esbuild -ErrorAction SilentlyContinue)) {
        throw "esbuild is required to refresh the Openmd Douyin plugin runtime. Install esbuild or repair the OpenCLI host tooling."
    }
    & esbuild $sourcePath "--outfile=$runtimePath" --format=esm --platform=node
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to transpile the Openmd Douyin plugin runtime."
    }
}

$compiledFresh = (
    (Test-Path -LiteralPath $runtimePath -PathType Leaf) -and
    (Get-Item -LiteralPath $runtimePath).LastWriteTimeUtc -ge (Get-Item -LiteralPath $sourcePath).LastWriteTimeUtc
)
if (-not $compiledFresh) {
    throw "Openmd Douyin plugin runtime is missing or stale. Rerun with -Install."
}

$pluginList = (& opencli plugin list 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "OpenCLI plugin list failed."
}

$installed = $pluginList -match '(?m)^\s*openmd-douyin\b'
if ($Install) {
    if ($installed) {
        & opencli plugin update openmd-douyin
        if ($LASTEXITCODE -ne 0) {
            throw "OpenCLI failed to update the registered Openmd Douyin plugin."
        }
    } else {
        $pluginUri = [System.Uri]::new((Resolve-Path -LiteralPath $pluginDir).Path).AbsoluteUri
        & opencli plugin install $pluginUri
        if ($LASTEXITCODE -ne 0) {
            throw "OpenCLI failed to install the Openmd Douyin plugin."
        }
    }
    $pluginList = (& opencli plugin list 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "OpenCLI plugin list failed after install/update."
    }
    $installed = $pluginList -match '(?m)^\s*openmd-douyin\b'
}

if (-not $installed) {
    throw "Openmd Douyin plugin is not registered. Rerun with -Install."
}

$commandHelp = (& opencli openmd-douyin creator-videos --help 2>&1 | Out-String)
$commandReady = $LASTEXITCODE -eq 0 -and $commandHelp -match 'Usage:\s+opencli openmd-douyin creator-videos'
if (-not $commandReady) {
    throw "Openmd Douyin creator-videos command is registered but its help contract failed."
}

[ordered]@{
    ok = $true
    installed = $installed
    command_ready = $commandReady
    compiled_fresh = $compiledFresh
    plugin_dir = $pluginDir
    mode = if ($Install) { "install-and-check" } else { "check-only" }
} | ConvertTo-Json -Compress
