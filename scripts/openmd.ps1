param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $OpenmdArgs
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$srcPath = Join-Path $repoRoot "src"

$oldPythonPath = $env:PYTHONPATH
if ([string]::IsNullOrWhiteSpace($oldPythonPath)) {
    $env:PYTHONPATH = $srcPath
} else {
    $env:PYTHONPATH = "$srcPath;$oldPythonPath"
}

try {
    $localPython = Join-Path $repoRoot "out\runtime\Scripts\python.exe"
    $python = if ($env:OPENMD_PYTHON) { $env:OPENMD_PYTHON } elseif (Test-Path -LiteralPath $localPython) { $localPython } else { "python" }
    & $python -m openmd @OpenmdArgs
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPythonPath
}
