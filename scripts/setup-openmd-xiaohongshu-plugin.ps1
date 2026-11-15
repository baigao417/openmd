[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$pluginDir = Join-Path $repoRoot "integrations\opencli\openmd-xiaohongshu"
$sourcePath = Join-Path $pluginDir "creator-notes.ts"
$runtimePath = Join-Path $pluginDir "creator-notes.js"
$assetSourcePath = Join-Path $pluginDir "note-assets.ts"
$assetRuntimePath = Join-Path $pluginDir "note-assets.js"
$checkScript = Join-Path $scriptDir "check-openmd-xiaohongshu-plugin.ps1"

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

if (-not (Get-Command opencli -ErrorAction SilentlyContinue)) {
    throw "opencli is not available on PATH. Install or repair OpenCLI before registering the Openmd Xiaohongshu plugin."
}
if (-not (Get-Command esbuild -ErrorAction SilentlyContinue)) {
    throw "esbuild is required to build the Openmd Xiaohongshu plugin runtime."
}

& esbuild $sourcePath "--outfile=$runtimePath" --format=esm --platform=node
if ($LASTEXITCODE -ne 0) {
    throw "Failed to transpile the Openmd Xiaohongshu plugin runtime."
}
& esbuild $assetSourcePath "--outfile=$assetRuntimePath" --format=esm --platform=node
if ($LASTEXITCODE -ne 0) {
    throw "Failed to transpile the Openmd Xiaohongshu note-assets runtime."
}

$pluginList = (& opencli plugin list 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "OpenCLI plugin list failed."
}
$installed = $pluginList -match '(?m)^\s*openmd-xiaohongshu\b'
if ($installed) {
    & opencli plugin update openmd-xiaohongshu
    if ($LASTEXITCODE -ne 0) {
        throw "OpenCLI failed to update the registered Openmd Xiaohongshu plugin."
    }
} else {
    $pluginUri = [System.Uri]::new((Resolve-Path -LiteralPath $pluginDir).Path).AbsoluteUri
    & opencli plugin install $pluginUri
    if ($LASTEXITCODE -ne 0) {
        throw "OpenCLI failed to install the Openmd Xiaohongshu plugin."
    }
}

$checkJson = & $checkScript
if ($LASTEXITCODE -ne 0) {
    throw "Openmd Xiaohongshu plugin post-install check failed."
}
$check = $checkJson | ConvertFrom-Json
$check.mode = "install-and-check"
$check | ConvertTo-Json -Compress
