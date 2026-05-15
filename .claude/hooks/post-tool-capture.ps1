# Post-tool-use hook wrapper. The shared Node script is used for cross-platform
# behavior; this PowerShell wrapper remains for users who call it directly.
param()
$ErrorActionPreference = 'SilentlyContinue'

$nodeHook = Join-Path $PSScriptRoot "post-tool-capture.mjs"
$inputJson = [Console]::In.ReadToEnd()

if ((Get-Command node -ErrorAction SilentlyContinue) -and (Test-Path $nodeHook)) {
    $inputJson | node $nodeHook | Write-Output
    exit 0
}

exit 0
