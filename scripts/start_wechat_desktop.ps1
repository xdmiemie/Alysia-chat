param(
    [string]$Character = "elysia",
    [string]$Config = "configs/runtime/wechat.yaml",
    [switch]$SkipHealth,
    [switch]$EnableCalls,
    [switch]$Once,
    [switch]$Json,
    [string]$Qwen3Model = "",
    [string]$GPTSoVITSModel = "base-v1",
    [switch]$Qwen3Interactive,
    [switch]$GPTSoVITSInteractive,
    [switch]$NoServiceManagement,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython
$CallsEnabled = $EnableCalls -or ($RemainingArgs -contains "--enable-calls")

# Stage 17 normally owns the model-service lifecycle. Services that were
# already listening are reused and are deliberately not stopped on exit.
if ($CallsEnabled -and -not $NoServiceManagement) {
    $LauncherArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $ProjectRoot "scripts\start_app.ps1"),
        "-WeChatDesktop", "-EnableCalls",
        "-Character", $Character,
        "-WeChatConfig", $Config,
        "-Python", $Python
    )
    if (-not [string]::IsNullOrWhiteSpace($Qwen3Model)) {
        $LauncherArgs += @("-Qwen3Model", $Qwen3Model)
    }
    if (-not [string]::IsNullOrWhiteSpace($GPTSoVITSModel)) {
        $LauncherArgs += @("-GPTSoVITSModel", $GPTSoVITSModel)
    }
    if ($Qwen3Interactive) {
        $LauncherArgs += "-Qwen3Interactive"
    }
    if ($GPTSoVITSInteractive) {
        $LauncherArgs += "-GPTSoVITSInteractive"
    }
    if ($SkipHealth) {
        $LauncherArgs += "-SkipHealth"
    }
    if ($Once) {
        $LauncherArgs += "-Once"
    }
    if ($Json) {
        $LauncherArgs += "-Json"
    }
    Write-Host "Stage 17 managed startup: Qwen3 + GPT-SoVITS + in-process SenseVoice."
    & powershell @LauncherArgs
    exit $LASTEXITCODE
}

$Args = @("-m", "runtime.adapters.wechat_desktop.app", "--character-id", $Character, "--config", $Config)
if ($SkipHealth) {
    $Args += "--skip-health"
}
if ($CallsEnabled) {
    $Args += "--enable-calls"
}
if ($Once) {
    $Args += "--once"
}
if ($Json) {
    $Args += "--json"
}
if ($Args -contains "--enable-calls") {
    Write-Host "Stage 17 WeChat half-duplex call automation: enabled"
} else {
    Write-Host "WeChat call automation: disabled"
}
& $Python @Args
