param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $OpenmdArgs
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $scriptDir "openmd.ps1") @OpenmdArgs
exit $LASTEXITCODE
