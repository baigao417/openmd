[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$pluginDir = Join-Path $repoRoot "integrations\opencli\openmd-xiaohongshu"
$manifestPath = Join-Path $pluginDir "opencli-plugin.json"
$sourcePath = Join-Path $pluginDir "creator-notes.ts"
$runtimePath = Join-Path $pluginDir "creator-notes.js"
$assetSourcePath = Join-Path $pluginDir "note-assets.ts"
$assetRuntimePath = Join-Path $pluginDir "note-assets.js"

foreach ($path in @($manifestPath, $sourcePath, $runtimePath, $assetSourcePath, $assetRuntimePath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Openmd Xiaohongshu plugin file not found: $path"
    }
}

if (-not (Get-Command opencli -ErrorAction SilentlyContinue)) {
    throw "opencli is not available on PATH. Install or repair OpenCLI before checking the Openmd Xiaohongshu plugin."
}

$compiledFresh = (
    (Get-Item -LiteralPath $runtimePath).LastWriteTimeUtc -ge (Get-Item -LiteralPath $sourcePath).LastWriteTimeUtc -and
    (Get-Item -LiteralPath $assetRuntimePath).LastWriteTimeUtc -ge (Get-Item -LiteralPath $assetSourcePath).LastWriteTimeUtc
)
if (-not $compiledFresh) {
    throw "Openmd Xiaohongshu plugin runtime is stale. Run setup-openmd-xiaohongshu-plugin.ps1."
}

$pluginList = (& opencli plugin list 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "OpenCLI plugin list failed."
}
$installed = $pluginList -match '(?m)^\s*openmd-xiaohongshu\b'
if (-not $installed) {
    throw "Openmd Xiaohongshu plugin is not registered. Run setup-openmd-xiaohongshu-plugin.ps1."
}

$commandHelp = (& opencli openmd-xiaohongshu creator-notes --help 2>&1 | Out-String)
$commandReady = $LASTEXITCODE -eq 0 -and $commandHelp -match 'Usage:\s+opencli openmd-xiaohongshu creator-notes'
if (-not $commandReady) {
    throw "Openmd Xiaohongshu creator-notes command is registered but its help contract failed."
}
$assetHelp = (& opencli openmd-xiaohongshu note-assets --help 2>&1 | Out-String)
$assetCommandReady = $LASTEXITCODE -eq 0 -and $assetHelp -match 'Usage:\s+opencli openmd-xiaohongshu note-assets'
if (-not $assetCommandReady) {
    throw "Openmd Xiaohongshu note-assets command is registered but its help contract failed."
}

[ordered]@{
    ok = $true
    installed = $installed
    command_ready = $commandReady
    asset_command_ready = $assetCommandReady
    compiled_fresh = $compiledFresh
    plugin_dir = $pluginDir
    mode = "check-only"
} | ConvertTo-Json -Compress
