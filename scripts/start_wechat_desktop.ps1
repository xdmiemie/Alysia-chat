param(
    [string]$Character = "elysia",
    [string]$Config = "configs/runtime/wechat.yaml",
    [switch]$SkipHealth,
    [switch]$Once,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython

$Args = @("-m", "runtime.adapters.wechat_desktop.app", "--character-id", $Character, "--config", $Config)
if ($SkipHealth) {
    $Args += "--skip-health"
}
if ($Once) {
    $Args += "--once"
}
if ($Json) {
    $Args += "--json"
}
& $Python @Args
