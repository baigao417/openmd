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
