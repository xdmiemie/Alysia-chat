param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path $ProjectRoot "storage\temporary\runtime-pids.json"
$Qwen3PidFile = Join-Path $ProjectRoot "storage\temporary\qwen3-llama-server.pid"
$Qwen3ModelPath = Join-Path $ProjectRoot "model_store\registry\qwen3\qwen3-8b\q4-k-m-base-v1\Qwen3-8B-Q4_K_M.gguf"

if (Test-Path $Qwen3PidFile) {
    $Qwen3Pid = [int](Get-Content -LiteralPath $Qwen3PidFile)
    try {
        Stop-Process -Id $Qwen3Pid -ErrorAction Stop
        Write-Host "Stopped Qwen3 llama-server process $Qwen3Pid"
    } catch {
        $ExistingProcess = Get-Process -Id $Qwen3Pid -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            Write-Host "Could not stop Qwen3 llama-server process ${Qwen3Pid}: $($_.Exception.Message)"
        } else {
            Write-Host "Qwen3 llama-server process $Qwen3Pid was not running."
        }
    }
    Remove-Item -LiteralPath $Qwen3PidFile -Force -ErrorAction SilentlyContinue
}

$Qwen3Processes = Get-CimInstance Win32_Process -Filter "name = 'llama-server.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$Qwen3ModelPath*" }

foreach ($Process in $Qwen3Processes) {
    try {
        Stop-Process -Id ([int]$Process.ProcessId) -ErrorAction Stop
        Write-Host "Stopped Qwen3 llama-server process $($Process.ProcessId)"
    } catch {
        Write-Host "Could not stop Qwen3 llama-server process $($Process.ProcessId): $($_.Exception.Message)"
    }
}

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
