param(
    [string]$Config = "configs/runtime/wechat.yaml"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ConfigPath = Join-Path $ProjectRoot $Config

Write-Host "Stage 11 Windows WeChat calibration checklist"
Write-Host ""
Write-Host "1. Open Windows WeChat with the dedicated character account."
Write-Host "2. Keep the WeChat main window visible and unlocked."
Write-Host "3. Pin or keep the test contact visible in the recent chat list."
Write-Host "4. Add the stable contact key or display name to configs/runtime/wechat.yaml."
Write-Host "5. Confirm the current Windows WeChat version and selector profile after real UI validation."
Write-Host ""
Write-Host "Config: $ConfigPath"
Write-Host ""
Write-Host "This script does not click or send anything. It is a safe pre-flight reminder for Stage 11."
