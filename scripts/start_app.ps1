param(
    [string]$Character = "elysia",
    [int]$MaxTurns = 0,
    [switch]$SkipHealth
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$CondaPrefixPython = Join-Path $ProjectRoot ".venv\python.exe"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PidFile = Join-Path $ProjectRoot "storage\temporary\stage6-started-pids.json"
$QwenPidFile = Join-Path $ProjectRoot "storage\temporary\qwen3-llama-server.pid"
$GptStdoutLog = Join-Path $ProjectRoot "storage\logs\gpt-sovits-upstream.out.log"
$GptStderrLog = Join-Path $ProjectRoot "storage\logs\gpt-sovits-upstream.err.log"

if (Test-Path $CondaPrefixPython) {
    $Python = $CondaPrefixPython
} elseif (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = "python"
}

function Test-TcpPort {
    param([string]$HostName, [int]$Port)
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Connect = $Client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $Connect.AsyncWaitHandle.WaitOne(1000, $false)) {
            return $false
        }
        $Client.EndConnect($Connect)
        return $true
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

function Wait-TcpPort {
    param([string]$HostName, [int]$Port, [string]$Name, [int]$TimeoutSeconds = 120)
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $Deadline) {
        if (Test-TcpPort -HostName $HostName -Port $Port) {
            Write-Host "$Name is ready at ${HostName}:${Port}."
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "$Name did not become reachable at ${HostName}:${Port} within $TimeoutSeconds seconds."
}

function Stop-StartedProcessTree {
    param([int]$ProcessId, [string]$Name)
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $Process) {
        Write-Host "$Name process $ProcessId was already stopped."
        return
    }
    & taskkill.exe /PID $ProcessId /T /F | Out-Null
    Write-Host "Stopped $Name process tree $ProcessId."
}

New-Item -ItemType Directory -Force -Path (Split-Path $PidFile) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $GptStdoutLog) | Out-Null
$Started = @()

try {
    if (Test-TcpPort -HostName "127.0.0.1" -Port 8080) {
        Write-Host "Reusing existing Qwen3 llama-server at 127.0.0.1:8080."
    } else {
        Write-Host "Starting Qwen3 llama-server..."
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\start_qwen3_server.ps1") -Background
        Wait-TcpPort -HostName "127.0.0.1" -Port 8080 -Name "Qwen3 llama-server"
        if (Test-Path $QwenPidFile) {
            $QwenPid = [int](Get-Content -LiteralPath $QwenPidFile)
            $Started += [pscustomobject]@{ name = "Qwen3 llama-server"; pid = $QwenPid }
        }
    }

    if (Test-TcpPort -HostName "127.0.0.1" -Port 9880) {
        Write-Host "Reusing existing GPT-SoVITS upstream at 127.0.0.1:9880."
    } else {
        Write-Host "Starting GPT-SoVITS upstream API..."
        $GptScript = Join-Path $ProjectRoot "scripts\start_gpt_sovits_upstream.ps1"
        $GptArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $GptScript, "-BindAddr", "127.0.0.1", "-Port", "9880")
        $GptProcess = Start-Process `
            -FilePath "powershell" `
            -ArgumentList $GptArgs `
            -WindowStyle Hidden `
            -RedirectStandardOutput $GptStdoutLog `
            -RedirectStandardError $GptStderrLog `
            -PassThru
        $Started += [pscustomobject]@{ name = "GPT-SoVITS upstream"; pid = $GptProcess.Id }
        Wait-TcpPort -HostName "127.0.0.1" -Port 9880 -Name "GPT-SoVITS upstream"
    }

    $Started | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8

    $AppArgs = @("-m", "runtime.app.main", "--character-id", $Character)
    if ($MaxTurns -gt 0) {
        $AppArgs += @("--max-turns", "$MaxTurns")
    }
    if ($SkipHealth) {
        $AppArgs += "--skip-health"
    }

    Push-Location $ProjectRoot
    try {
        & $Python @AppArgs
    } finally {
        Pop-Location
    }
} finally {
    foreach ($Entry in $Started) {
        Stop-StartedProcessTree -ProcessId ([int]$Entry.pid) -Name ([string]$Entry.name)
        if ($Entry.name -eq "Qwen3 llama-server") {
            Remove-Item -LiteralPath $QwenPidFile -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}
