param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path $ProjectRoot "storage\temporary\runtime-pids.json"

if (-not (Test-Path $PidFile)) {
    Write-Host "No runtime pid file found. Nothing to stop."
    exit 0
}

$Pids = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json
foreach ($RuntimePid in $Pids) {
    try {
        Stop-Process -Id ([int]$RuntimePid) -ErrorAction Stop
        Write-Host "Stopped process $RuntimePid"
    } catch {
        Write-Host "Process $RuntimePid was not running."
    }
}

Remove-Item -LiteralPath $PidFile -Force
