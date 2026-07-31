param(
    [string]$Character = "elysia",
    [string]$Config = "configs/runtime/wechat.yaml",
    [switch]$SkipHealth,
    [switch]$EnableCalls,
    [switch]$Once,
    [switch]$Json,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython

$Args = @("-m", "runtime.adapters.wechat_desktop.app", "--character-id", $Character, "--config", $Config)
if ($SkipHealth) {
    $Args += "--skip-health"
}
if ($EnableCalls -or ($RemainingArgs -contains "--enable-calls")) {
    $Args += "--enable-calls"
}
if ($Once) {
    $Args += "--once"
}
if ($Json) {
    $Args += "--json"
}
if ($Args -contains "--enable-calls") {
    Write-Host "Stage 16 WeChat call automation: enabled"
} else {
    Write-Host "Stage 16 WeChat call automation: disabled"
}
& $Python @Args
