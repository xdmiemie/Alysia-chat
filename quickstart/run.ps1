param(
    [string]$Character = "",
    [string]$Qwen3Model = "",
    [string]$GPTSoVITSModel = "",
    [int]$MaxTurns = 0,
    [string]$Python = "",
    [switch]$Qwen3Interactive,
    [switch]$GPTSoVITSInteractive,
    [switch]$SkipHealth,
    [switch]$EnableQQ,
    [string]$QQPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ($EnableQQ) {
    $QQParams = @{}
    if ($QQPath) { $QQParams.QQPath = $QQPath }
    & (Join-Path $ProjectRoot "scripts\start_snowluma.ps1") @QQParams
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
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\start_app.ps1") @StartArgs
} finally {
    & (Join-Path $ProjectRoot "scripts\stop_qwen3_server.ps1")
    & (Join-Path $ProjectRoot "scripts\stop_gpt_sovits_upstream.ps1")
}
