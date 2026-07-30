param(
    [string]$Character = "elysia",
    [string]$Config = "configs/runtime/wechat.yaml",
    [switch]$Once,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython

$Args = @("-m", "runtime.adapters.wechat.app", "--provider", "ilink", "--character-id", $Character, "--config", $Config)
if ($Once) { $Args += "--once" }
if ($Json) { $Args += "--json" }
& $Python @Args
