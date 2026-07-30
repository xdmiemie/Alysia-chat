param(
    [switch]$AudioLoopback,
    [switch]$SenseVoice,
    [switch]$Qwen3,
    [switch]$LlamaServer,
    [string]$Character = "",
    [string]$Qwen3Model = "",
    [switch]$Qwen3Interactive,
    [switch]$GPTSoVITS,
    [switch]$GPTSoVITSWrapper,
    [switch]$WeChatBridge,
    [switch]$Doctor
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython

if ($Doctor -or (-not $AudioLoopback -and -not $SenseVoice -and -not $Qwen3 -and -not $LlamaServer -and -not $GPTSoVITS -and -not $GPTSoVITSWrapper -and -not $WeChatBridge)) {
    & $Python (Join-Path $ProjectRoot "scripts\doctor.py")
}

if ($AudioLoopback) {
    & $Python (Join-Path $ProjectRoot "scripts\audio_loopback.py")
}

if ($SenseVoice) {
    & $Python -m uvicorn runtime.sensevoice.api:app --host 127.0.0.1 --port 5001
}

if ($LlamaServer) {
    $QwenArgs = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $ProjectRoot "scripts\start_qwen3_server.ps1"), "-Background")
    if (-not [string]::IsNullOrWhiteSpace($Character)) {
        $QwenArgs += @("-Character", $Character)
    }
    if (-not [string]::IsNullOrWhiteSpace($Qwen3Model)) {
        $QwenArgs += @("-ModelVersion", $Qwen3Model)
    }
    if ($Qwen3Interactive) {
        $QwenArgs += "-Interactive"
    }
    & powershell @QwenArgs
}

if ($Qwen3) {
    $ChatArgs = @((Join-Path $ProjectRoot "scripts\qwen3_chat.py"))
    if (-not [string]::IsNullOrWhiteSpace($Character)) {
        $ChatArgs += @("--character", $Character)
    }
    & $Python @ChatArgs
}

if ($GPTSoVITS) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\start_gpt_sovits_upstream.ps1") -Python $Python
}

if ($GPTSoVITSWrapper) {
    & $Python -m uvicorn runtime.gpt_sovits.api:app --host 127.0.0.1 --port 5002
}

if ($WeChatBridge) {
    $BridgeArgs = @("-m", "runtime.adapters.wechat.app", "--provider", "ilink")
    if (-not [string]::IsNullOrWhiteSpace($Character)) {
        $BridgeArgs += @("--character-id", $Character)
    }
    & $Python @BridgeArgs
}
