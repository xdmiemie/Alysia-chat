param(
    [string]$Config = "configs/runtime/wechat.yaml",
    [string]$SenseVoiceConfig = "configs/runtime/sensevoice.yaml",
    [double]$CaptureSeconds = 12,
    [string]$CapturedWav = "",
    [switch]$SkipTranscribe,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython

$Args = @(
    (Join-Path $ProjectRoot "scripts\wechat_voice_capture_acceptance.py"),
    "--config", $Config,
    "--sensevoice-config", $SenseVoiceConfig,
    "--capture-seconds", "$CaptureSeconds"
)

if (-not [string]::IsNullOrWhiteSpace($CapturedWav)) {
    $Args += @("--captured-wav", $CapturedWav)
}
if ($SkipTranscribe) {
    $Args += "--skip-transcribe"
}
if ($Json) {
    $Args += "--json"
}

& $Python @Args
