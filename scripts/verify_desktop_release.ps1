[CmdletBinding()]
param(
    [string]$TargetTriple = "x86_64-pc-windows-msvc",
    [ValidateSet("nsis")]
    [string]$Bundle = "nsis"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BinaryDir = Join-Path $RepoRoot "apps\desktop\src-tauri\binaries"
$ManifestPath = Join-Path $BinaryDir "sora-gateway-manifest.json"
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Gateway sidecar manifest is missing: $ManifestPath"
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Manifest.schema_version -ne "SoraGatewaySidecarManifest.v1") {
    throw "Unexpected Gateway sidecar manifest schema"
}
if ($Manifest.protocol_version -ne "1.0" -or $Manifest.target -ne $TargetTriple) {
    throw "Gateway sidecar manifest protocol or target mismatch"
}
if ($Manifest.sha256 -notmatch '^[0-9a-f]{64}$' -or [int64]$Manifest.size_bytes -le 0) {
    throw "Gateway sidecar manifest integrity metadata is invalid"
}

$BinaryPath = Join-Path $BinaryDir $Manifest.file_name
if (-not (Test-Path -LiteralPath $BinaryPath -PathType Leaf)) {
    throw "Gateway sidecar binary is missing: $BinaryPath"
}
$Binary = Get-Item -LiteralPath $BinaryPath
$ActualHash = (Get-FileHash -LiteralPath $BinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($Binary.Length -ne [int64]$Manifest.size_bytes -or $ActualHash -ne $Manifest.sha256) {
    throw "Gateway sidecar binary does not match its build manifest"
}

$DesktopPackage = Get-Content -LiteralPath (Join-Path $RepoRoot "apps\desktop\package.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$InstallerPath = Join-Path $RepoRoot "apps\desktop\src-tauri\target\release\bundle\$Bundle\Project Sora_$($DesktopPackage.version)_x64-setup.exe"
if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    throw "Desktop installer is missing: $InstallerPath"
}
$Installer = Get-Item -LiteralPath $InstallerPath
if ($Installer.Length -lt 1MB) {
    throw "Desktop installer is unexpectedly small: $($Installer.Length) bytes"
}

$PythonExecutable = Join-Path $RepoRoot ".venv\Scripts\python.exe"
& $PythonExecutable (Join-Path $PSScriptRoot "verify_packaged_gateway.py") --gateway $BinaryPath
if ($LASTEXITCODE -ne 0) {
    throw "Packaged Gateway startup/authentication/shutdown verification failed"
}

Write-Host "Desktop release verified"
Write-Host "  sidecar: $($Binary.Name) ($($Binary.Length) bytes)"
Write-Host "  installer: $($Installer.Name) ($($Installer.Length) bytes)"
