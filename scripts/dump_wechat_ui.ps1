param(
    [string]$Output = "storage/logs/wechat_desktop/ui_dump.jsonl",
    [int]$Limit = 800
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AlysiaRuntimePython

& $Python (Join-Path $ProjectRoot "scripts\dump_wechat_ui.py") --output $Output --limit $Limit
