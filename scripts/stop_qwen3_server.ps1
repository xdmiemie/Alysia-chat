param()

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidFile = Join-Path $ProjectRoot "storage\temporary\qwen3-llama-server.pid"
$BinaryRoot = (Resolve-Path (Join-Path $ProjectRoot "tools\llama.cpp")).Path.TrimEnd('\') + '\'

if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) { return }
$Marker = Get-Item -LiteralPath $PidFile
$ProcessId = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$ProcessId)) {
    throw "Invalid Qwen3 PID marker: $PidFile"
}
$ModelProcess = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if (-not $ModelProcess) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "Qwen3 was already stopped."
    return
}
$Started = $ModelProcess.StartTime.ToUniversalTime()
$Marked = $Marker.LastWriteTimeUtc
if ($ModelProcess.ProcessName -ne "llama-server" -or
    -not $ModelProcess.Path.StartsWith($BinaryRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $Started -gt $Marked.AddSeconds(3) -or $Started -lt $Marked.AddSeconds(-30)) {
    throw "Qwen3 PID marker does not match the running project model process; refusing to stop PID $ProcessId."
}
Stop-Process -Id $ProcessId -Force -ErrorAction Stop
Remove-Item -LiteralPath $PidFile -Force
Write-Host "Stopped project Qwen3 llama-server process $ProcessId."
