$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Marker = Join-Path $ProjectRoot "storage\temporary\snowluma-owned.json"
if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) {
    Write-Host "No project-owned SnowLuma process is recorded."
    return
}
$Saved = Get-Content -LiteralPath $Marker -Raw | ConvertFrom-Json
$Process = Get-Process -Id ([int]$Saved.pid) -ErrorAction SilentlyContinue
if (-not $Process) {
    Remove-Item -LiteralPath $Marker -Force
    Write-Host "SnowLuma was already stopped."
    return
}
$Difference = [Math]::Abs(($Process.StartTime.ToUniversalTime() - [DateTime]::Parse([string]$Saved.started_at_utc).ToUniversalTime()).TotalSeconds)
if ($Process.Path -ne [string]$Saved.executable -or $Difference -gt 3) {
    throw "The recorded PID now belongs to a different process. Refusing to stop it."
}
& taskkill.exe /PID $Process.Id /T /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not stop SnowLuma process $($Process.Id)." }
Remove-Item -LiteralPath $Marker -Force
Write-Host "Stopped project-owned SnowLuma process $($Process.Id). QQ was left running."
