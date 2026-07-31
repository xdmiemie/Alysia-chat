param(
    [string]$Config = "configs/runtime/wechat.yaml",
    [string]$Output = "storage/logs/wechat_desktop/call_windows_dump.jsonl",
    [int]$LimitWindows = 80,
    [int]$LimitDescendants = 500,
    [switch]$IncludeAllProcesses
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython

$Args = @(
    (Join-Path $ProjectRoot "scripts\dump_wechat_call_windows.py"),
    "--config", $Config,
    "--output", $Output,
    "--limit-windows", $LimitWindows,
    "--limit-descendants", $LimitDescendants
)
if ($IncludeAllProcesses) {
    $Args += "--include-all-processes"
}

& $Python @Args
