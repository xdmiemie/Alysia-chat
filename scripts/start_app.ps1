param(
    [string]$Character = "",
    [string]$Qwen3Model = "",
    [string]$GPTSoVITSModel = "",
    [int]$MaxTurns = 0,
    [string]$Python = "",
    [switch]$Qwen3Interactive,
    [switch]$GPTSoVITSInteractive,
    [switch]$SkipHealth
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path $ProjectRoot "storage\temporary\stage6-started-pids.json"
$QwenPidFile = Join-Path $ProjectRoot "storage\temporary\qwen3-llama-server.pid"
$GptCharacterFile = Join-Path $ProjectRoot "storage\temporary\gpt-sovits-upstream.character"
$GptModelFile = Join-Path $ProjectRoot "storage\temporary\gpt-sovits-upstream.model"
$GptStdoutLog = Join-Path $ProjectRoot "storage\logs\gpt-sovits-upstream.out.log"
$GptStderrLog = Join-Path $ProjectRoot "storage\logs\gpt-sovits-upstream.err.log"

. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$RuntimePython = Resolve-AstrbotChatRuntimePython -PythonOverride $Python

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
    $QwenRunning = Test-TcpPort -HostName "127.0.0.1" -Port 8080
    if ($QwenRunning) {
        $ResolveArgs = @((Join-Path $ProjectRoot "scripts\qwen3_models.py"), "resolve", "--json")
        if (-not [string]::IsNullOrWhiteSpace($Character)) {
            $ResolveArgs += @("--character", $Character)
        }
        if (-not [string]::IsNullOrWhiteSpace($Qwen3Model)) {
            $ResolveArgs += @("--model", $Qwen3Model)
        }
        $ResolvedJson = & $RuntimePython @ResolveArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Could not resolve current Qwen3 model selection."
        }
        $ResolvedQwen3 = $ResolvedJson | ConvertFrom-Json
        $Character = [string]$ResolvedQwen3.character_id
        $Qwen3Model = [string]$ResolvedQwen3.model_id
        Write-Host "Reusing existing Qwen3 llama-server at 127.0.0.1:8080."
    } else {
        $ResolveArgs = @((Join-Path $ProjectRoot "scripts\qwen3_models.py"), "resolve", "--json", "--activate")
        if ($Qwen3Interactive -or [string]::IsNullOrWhiteSpace($Qwen3Model)) {
            $ResolveArgs += "--interactive"
        }
        if (-not [string]::IsNullOrWhiteSpace($Character)) {
            $ResolveArgs += @("--character", $Character)
        }
        if (-not [string]::IsNullOrWhiteSpace($Qwen3Model)) {
            $ResolveArgs += @("--model", $Qwen3Model)
        }
        $ResolvedJson = & $RuntimePython @ResolveArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Could not resolve Qwen3 model selection."
        }
        $ResolvedQwen3 = $ResolvedJson | ConvertFrom-Json
        $Character = [string]$ResolvedQwen3.character_id
        $Qwen3Model = [string]$ResolvedQwen3.model_id

        Write-Host "Starting Qwen3 llama-server..."
        $QwenArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ProjectRoot "scripts\start_qwen3_server.ps1"), "-Background", "-Character", $Character)
        if (-not [string]::IsNullOrWhiteSpace($Qwen3Model)) {
            $QwenArgs += @("-ModelVersion", $Qwen3Model)
        }
        & powershell @QwenArgs
        Wait-TcpPort -HostName "127.0.0.1" -Port 8080 -Name "Qwen3 llama-server"
        if (Test-Path $QwenPidFile) {
            $QwenPid = [int](Get-Content -LiteralPath $QwenPidFile)
            $Started += [pscustomobject]@{ name = "Qwen3 llama-server"; pid = $QwenPid }
        }
    }

    $ResolveGptArgs = @((Join-Path $ProjectRoot "scripts\gpt_sovits_models.py"), "resolve", "--json", "--activate", "--character", $Character)
    if ($GPTSoVITSInteractive -or [string]::IsNullOrWhiteSpace($GPTSoVITSModel)) {
        $ResolveGptArgs += "--interactive"
    }
    if (-not [string]::IsNullOrWhiteSpace($GPTSoVITSModel)) {
        $ResolveGptArgs += @("--model", $GPTSoVITSModel)
    }
    $ResolvedGptJson = & $RuntimePython @ResolveGptArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Could not resolve GPT-SoVITS model selection."
    }
    $ResolvedGpt = $ResolvedGptJson | ConvertFrom-Json
    $GPTSoVITSModel = [string]$ResolvedGpt.model_id
    Write-Host "Using GPT-SoVITS model: $Character/$GPTSoVITSModel"

    if (Test-TcpPort -HostName "127.0.0.1" -Port 9880) {
        if (Test-Path -LiteralPath $GptCharacterFile) {
            $RunningGptCharacter = (Get-Content -LiteralPath $GptCharacterFile -Raw).Trim()
            if ($RunningGptCharacter -and $RunningGptCharacter -ne $Character) {
                throw "GPT-SoVITS upstream is already running for '$RunningGptCharacter', but selected character is '$Character'. Stop it first with scripts/stop_runtime.ps1, then start again."
            }
        } else {
            throw "GPT-SoVITS upstream is already running, but its character marker is missing. Stop it first with scripts/stop_runtime.ps1, then start again."
        }
        if (Test-Path -LiteralPath $GptModelFile) {
            $RunningGptModel = (Get-Content -LiteralPath $GptModelFile -Raw).Trim()
            if ($RunningGptModel -and $RunningGptModel -ne $GPTSoVITSModel) {
                throw "GPT-SoVITS upstream is already running with model '$RunningGptModel', but selected model is '$GPTSoVITSModel'. Stop it first with scripts/stop_runtime.ps1, then start again."
            }
        } else {
            throw "GPT-SoVITS upstream is already running, but its model marker is missing. Stop it first with scripts/stop_runtime.ps1, then start again."
        }
        Write-Host "Reusing existing GPT-SoVITS upstream at 127.0.0.1:9880."
    } else {
        Write-Host "Starting GPT-SoVITS upstream API..."
        $GptScript = Join-Path $ProjectRoot "scripts\start_gpt_sovits_upstream.ps1"
        $GptArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $GptScript, "-Python", $RuntimePython, "-BindAddr", "127.0.0.1", "-Port", "9880", "-Character", $Character, "-Model", $GPTSoVITSModel)
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
        Write-Host "Using unified runtime Python: $RuntimePython"
        & $RuntimePython @AppArgs
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
    if (-not (Test-TcpPort -HostName "127.0.0.1" -Port 9880)) {
        Remove-Item -LiteralPath $GptCharacterFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $GptModelFile -Force -ErrorAction SilentlyContinue
    }
}
