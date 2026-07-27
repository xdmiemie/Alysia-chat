param(
    [string]$Character = "",
    [string]$Qwen3Model = "",
    [string]$GPTSoVITSModel = "",
    [int]$MaxTurns = 0,
    [string]$Python = "",
    [switch]$Qwen3Interactive,
    [switch]$GPTSoVITSInteractive,
    [switch]$SkipBootstrap,
    [switch]$LightweightBootstrap,
    [switch]$SkipLfsPull,
    [switch]$SkipThirdParty,
    [switch]$SkipModelDownloads,
    [switch]$SkipHealth
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not $SkipBootstrap) {
    $BootstrapArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($Python)) {
        $BootstrapArgs += @("-Python", $Python)
    }
    if ($LightweightBootstrap) {
        $BootstrapArgs += "-Lightweight"
    }
    if ($SkipLfsPull) {
        $BootstrapArgs += "-SkipLfsPull"
    }
    if ($SkipThirdParty) {
        $BootstrapArgs += "-SkipThirdParty"
    }
    if ($SkipModelDownloads) {
        $BootstrapArgs += "-SkipModelDownloads"
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "quickstart\bootstrap.ps1") @BootstrapArgs
}

$StartArgs = @()
if (-not [string]::IsNullOrWhiteSpace($Character)) {
    $StartArgs += @("-Character", $Character)
}
if (-not [string]::IsNullOrWhiteSpace($Qwen3Model)) {
    $StartArgs += @("-Qwen3Model", $Qwen3Model)
}
if (-not [string]::IsNullOrWhiteSpace($GPTSoVITSModel)) {
    $StartArgs += @("-GPTSoVITSModel", $GPTSoVITSModel)
}
if ($Qwen3Interactive -or [string]::IsNullOrWhiteSpace($Qwen3Model)) {
    $StartArgs += "-Qwen3Interactive"
}
if ($GPTSoVITSInteractive -or [string]::IsNullOrWhiteSpace($GPTSoVITSModel)) {
    $StartArgs += "-GPTSoVITSInteractive"
}
if (-not [string]::IsNullOrWhiteSpace($Python)) {
    $StartArgs += @("-Python", $Python)
}
if ($MaxTurns -gt 0) {
    $StartArgs += @("-MaxTurns", "$MaxTurns")
}
if ($SkipHealth) {
    $StartArgs += "-SkipHealth"
}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\start_app.ps1") @StartArgs
