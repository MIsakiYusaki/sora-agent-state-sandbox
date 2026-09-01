[CmdletBinding()]
param(
    [string]$PythonExecutable,
    [string]$TargetTriple = "x86_64-pc-windows-msvc"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $PythonExecutable) {
    $PythonExecutable = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Desktop sidecar Python executable was not found: $PythonExecutable"
}
if ($TargetTriple -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "Invalid Rust target triple: $TargetTriple"
}

$env:SORA_REPO_ROOT = $RepoRoot
$env:SORA_TARGET_TRIPLE = $TargetTriple
$BinaryDir = Join-Path $RepoRoot "apps\desktop\src-tauri\binaries"
$WorkDir = Join-Path $RepoRoot "build\pyinstaller\sora-gateway"
$SpecPath = Join-Path $RepoRoot "packaging\sora_gateway.spec"
New-Item -ItemType Directory -Force -Path $BinaryDir, $WorkDir | Out-Null

& $PythonExecutable -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is unavailable. Install the desktop-build extra before packaging."
}

& $PythonExecutable -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $BinaryDir `
    --workpath $WorkDir `
    $SpecPath
if ($LASTEXITCODE -ne 0) {
    throw "Gateway sidecar build failed with exit code $LASTEXITCODE"
}

$BinaryName = "sora-gateway-$TargetTriple.exe"
$BinaryPath = Join-Path $BinaryDir $BinaryName
if (-not (Test-Path -LiteralPath $BinaryPath -PathType Leaf)) {
    throw "Gateway sidecar output is missing: $BinaryPath"
}

$PackageVersion = (& $PythonExecutable -c "import importlib.metadata; print(importlib.metadata.version('sora-agent-state-sandbox'))").Trim()
if ($LASTEXITCODE -ne 0 -or -not $PackageVersion) {
    throw "Could not resolve the Project Sora package version"
}
$File = Get-Item -LiteralPath $BinaryPath
$Manifest = [ordered]@{
    schema_version = "SoraGatewaySidecarManifest.v1"
    version = $PackageVersion
    protocol_version = "1.0"
    target = $TargetTriple
    file_name = $BinaryName
    sha256 = (Get-FileHash -LiteralPath $BinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    size_bytes = $File.Length
}
$ManifestPath = Join-Path $BinaryDir "sora-gateway-manifest.json"
$ManifestJson = $Manifest | ConvertTo-Json
$Utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($ManifestPath, $ManifestJson, $Utf8WithoutBom)
Write-Host "Gateway sidecar ready: $BinaryPath"
Write-Host "Manifest: $ManifestPath"
