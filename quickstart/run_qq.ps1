param(
    [string]$QQPath = "",
    [string]$Character = "elysia",
    [string]$Qwen3Model = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$RuntimePython = Resolve-AlysiaRuntimePython -PythonOverride $Python

function Test-LocalPort {
    param([int]$Port)
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Connect = $Client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $Connect.AsyncWaitHandle.WaitOne(500, $false)) { return $false }
        $Client.EndConnect($Connect)
        return $true
    } catch { return $false } finally { $Client.Close() }
}

Push-Location $ProjectRoot
try {
    $SnowParams = @{}
    if ($QQPath) { $SnowParams.QQPath = $QQPath }
    & (Join-Path $ProjectRoot "scripts\start_snowluma.ps1") @SnowParams

    $ResolveArgs = @((Join-Path $ProjectRoot "scripts\qwen3_models.py"), "resolve", "--json", "--activate", "--character", $Character)
    if ($Qwen3Model) { $ResolveArgs += @("--model", $Qwen3Model) }
    $ResolvedJson = & $RuntimePython @ResolveArgs
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve Qwen3 model." }
    $Resolved = $ResolvedJson | ConvertFrom-Json
    if (-not (Test-LocalPort -Port 8080)) {
        $QwenArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ProjectRoot "scripts\start_qwen3_server.ps1"), "-Background", "-Character", [string]$Resolved.character_id, "-ModelVersion", [string]$Resolved.model_id)
        & powershell.exe @QwenArgs
        if ($LASTEXITCODE -ne 0) { throw "Could not start Qwen3 llama-server." }
    }
    $Deadline = (Get-Date).AddSeconds(180)
    while (-not (Test-LocalPort -Port 8080)) {
        if ((Get-Date) -gt $Deadline) { throw "Qwen3 llama-server did not become ready." }
        Start-Sleep -Seconds 2
    }
    Write-Host "Qwen3 ready. Connecting to SnowLuma OneBot; press Ctrl+C to stop QQ chat."
    & $RuntimePython (Join-Path $ProjectRoot "scripts\qq_chat.py")
    if ($LASTEXITCODE -ne 0) { throw "QQ chat runner exited with code $LASTEXITCODE." }
} finally {
    & (Join-Path $ProjectRoot "scripts\stop_qwen3_server.ps1")
    Pop-Location
}
