param()

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$MarkerFile = Join-Path $ProjectRoot "storage\temporary\gpt-sovits-upstream-owned.json"
if (-not (Test-Path -LiteralPath $MarkerFile -PathType Leaf)) { return }
$Saved = Get-Content -LiteralPath $MarkerFile -Raw | ConvertFrom-Json
$ModelProcess = Get-Process -Id ([int]$Saved.pid) -ErrorAction SilentlyContinue
if (-not $ModelProcess) {
    Remove-Item -LiteralPath $MarkerFile -Force
    Write-Host "GPT-SoVITS upstream was already stopped."
    return
}
$Started = [DateTime]::Parse([string]$Saved.started_at_utc).ToUniversalTime()
$Difference = [Math]::Abs(($ModelProcess.StartTime.ToUniversalTime() - $Started).TotalSeconds)
if ($ModelProcess.Path -ne [string]$Saved.executable -or $Difference -gt 3) {
    throw "GPT-SoVITS ownership marker does not match PID $($ModelProcess.Id); refusing to stop it."
}
& taskkill.exe /PID $ModelProcess.Id /T /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not stop project GPT-SoVITS upstream process $($ModelProcess.Id)." }
Remove-Item -LiteralPath $MarkerFile -Force
Write-Host "Stopped project GPT-SoVITS upstream process $($ModelProcess.Id)."
