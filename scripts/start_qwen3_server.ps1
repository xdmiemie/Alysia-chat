param(
    [string]$LlamaExecutable = "",
    [string]$Character = "",
    [string]$ModelVersion = "",
    [switch]$Interactive,
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path $ProjectRoot "storage\temporary\qwen3-llama-server.pid"
$StdoutLog = Join-Path $ProjectRoot "storage\logs\qwen3-llama-server.out.log"
$StderrLog = Join-Path $ProjectRoot "storage\logs\qwen3-llama-server.err.log"

. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
$Python = Resolve-AstrbotChatRuntimePython

$ResolveArgs = @((Join-Path $ProjectRoot "scripts\qwen3_models.py"), "resolve", "--json")
$PromptForModel = $Interactive -or ((-not $Background) -and [string]::IsNullOrWhiteSpace($Character) -and [string]::IsNullOrWhiteSpace($ModelVersion))
if ($PromptForModel) {
    $ResolveArgs += @("--interactive", "--activate")
    if (-not [string]::IsNullOrWhiteSpace($Character)) {
        $ResolveArgs += @("--character", $Character)
    }
} else {
    if (-not [string]::IsNullOrWhiteSpace($Character)) {
        $ResolveArgs += @("--character", $Character)
    }
    if (-not [string]::IsNullOrWhiteSpace($ModelVersion)) {
        $ResolveArgs += @("--model", $ModelVersion)
    }
    if (-not [string]::IsNullOrWhiteSpace($Character) -or -not [string]::IsNullOrWhiteSpace($ModelVersion)) {
        $ResolveArgs += "--activate"
    }
}

$ResolvedJson = & $Python @ResolveArgs
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve Qwen3 model selection."
}
$Resolved = $ResolvedJson | ConvertFrom-Json
$ModelPath = Join-Path $ProjectRoot $Resolved.runtime.model_file
$LoraAdapter = $null
if ($Resolved.runtime.PSObject.Properties.Name -contains "lora_adapter") {
    $LoraAdapter = Join-Path $ProjectRoot $Resolved.runtime.lora_adapter
}

if (-not (Test-Path -LiteralPath $ModelPath)) {
    throw "Qwen3 GGUF model not found: $ModelPath"
}
if ($LoraAdapter -and -not (Test-Path -LiteralPath $LoraAdapter)) {
    throw "Qwen3 LoRA adapter not found: $LoraAdapter"
}
if ($LoraAdapter -and ([IO.Path]::GetExtension($LoraAdapter).ToLowerInvariant() -ne ".gguf")) {
    throw "Qwen3 LoRA adapter must be GGUF for llama.cpp: $LoraAdapter. Run quickstart/convert_qwen3_lora_to_gguf.ps1 first."
}

if ([string]::IsNullOrWhiteSpace($LlamaExecutable)) {
    $LlamaToolRoot = Join-Path $ProjectRoot "tools\llama.cpp"
    $LlamaExecutable = Get-ChildItem -LiteralPath $LlamaToolRoot -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $LlamaExecutable) {
        throw "Could not find project llama-server under tools\llama.cpp. Run quickstart/bootstrap.ps1 first."
    }
}
if (-not (Test-Path -LiteralPath $LlamaExecutable)) {
    throw "llama-server executable does not exist: $LlamaExecutable"
}

$LlamaBinDir = Split-Path $LlamaExecutable -Parent
$env:Path = "$LlamaBinDir;$env:Path"
$ServerArgs = @(
    "--model", $ModelPath,
    "--host", "127.0.0.1",
    "--port", "8080",
    "--ctx-size", "8192",
    "--n-gpu-layers", "99"
)
if ($LoraAdapter) {
    $ServerArgs += @("--lora", $LoraAdapter)
}

if ($Background) {
    New-Item -ItemType Directory -Force -Path (Split-Path $PidFile) | Out-Null
    New-Item -ItemType Directory -Force -Path (Split-Path $StdoutLog) | Out-Null

    if (Test-Path $PidFile) {
        $ExistingPid = [int](Get-Content -LiteralPath $PidFile)
        $ExistingProcess = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            Write-Host "Qwen3 llama-server is already running with PID $ExistingPid."
            exit 0
        }
    }

    $Process = Start-Process `
        -FilePath $LlamaExecutable `
        -ArgumentList $ServerArgs `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -PassThru

    Set-Content -LiteralPath $PidFile -Value $Process.Id
    Write-Host "Started Qwen3 llama-server with PID $($Process.Id)."
    Write-Host "Using project llama-server: $LlamaExecutable"
    Write-Host "Using Qwen3 model: $($Resolved.character_id)/$($Resolved.model_id)"
    Write-Host "Logs: $StderrLog"
} else {
    Write-Host "Using project llama-server: $LlamaExecutable"
    Write-Host "Using Qwen3 model: $($Resolved.character_id)/$($Resolved.model_id)"
    & $LlamaExecutable @ServerArgs
}
