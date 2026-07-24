param(
    [switch]$AudioLoopback,
    [switch]$SenseVoice,
    [switch]$Qwen3,
    [switch]$LlamaServer,
    [switch]$GPTSoVITS,
    [switch]$GPTSoVITSWrapper,
    [switch]$Doctor
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython

if ($Doctor -or (-not $AudioLoopback -and -not $SenseVoice -and -not $Qwen3 -and -not $LlamaServer -and -not $GPTSoVITS -and -not $GPTSoVITSWrapper)) {
    & $Python (Join-Path $ProjectRoot "scripts\doctor.py")
}

if ($AudioLoopback) {
    & $Python (Join-Path $ProjectRoot "scripts\audio_loopback.py")
}

if ($SenseVoice) {
    & $Python -m uvicorn runtime.sensevoice.api:app --host 127.0.0.1 --port 5001
}

if ($LlamaServer) {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\start_qwen3_server.ps1") -Background
}

if ($Qwen3) {
    & $Python (Join-Path $ProjectRoot "scripts\qwen3_chat.py")
}

if ($GPTSoVITS) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\start_gpt_sovits_upstream.ps1") -Python $Python
}

if ($GPTSoVITSWrapper) {
    & $Python -m uvicorn runtime.gpt_sovits.api:app --host 127.0.0.1 --port 5002
}
