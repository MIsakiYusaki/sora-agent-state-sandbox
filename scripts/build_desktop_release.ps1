[CmdletBinding()]
param(
    [ValidateSet("nsis", "msi", "all")]
    [string]$Bundle = "all"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
& (Join-Path $PSScriptRoot "build_desktop_sidecar.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Gateway sidecar build failed"
}

$Pnpm = $env:SORA_PNPM_EXECUTABLE
if (-not $Pnpm) {
    $Pnpm = (Get-Command pnpm.cmd -ErrorAction Stop).Source
}
$Arguments = @(
    "--dir", (Join-Path $RepoRoot "apps\desktop"),
    "tauri", "build",
    "--config", "src-tauri/tauri.release.conf.json"
)
if ($Bundle -ne "all") {
    $Arguments += @("--bundles", $Bundle)
}
& $Pnpm @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "Tauri release build failed with exit code $LASTEXITCODE"
}
