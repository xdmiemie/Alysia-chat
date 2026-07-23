param(
    [string]$LlamaCommand = "llama-server",
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ModelPath = Join-Path $ProjectRoot "model_store\registry\qwen3\qwen3-8b\q4-k-m-base-v1\Qwen3-8B-Q4_K_M.gguf"
$PidFile = Join-Path $ProjectRoot "storage\temporary\qwen3-llama-server.pid"
$StdoutLog = Join-Path $ProjectRoot "storage\logs\qwen3-llama-server.out.log"
$StderrLog = Join-Path $ProjectRoot "storage\logs\qwen3-llama-server.err.log"
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")

if (-not (Test-Path $ModelPath)) {
    throw "Qwen3 GGUF model not found: $ModelPath"
}

$ResolvedLlamaCommand = Get-Command $LlamaCommand -ErrorAction SilentlyContinue
if ($ResolvedLlamaCommand) {
    $LlamaExecutable = $ResolvedLlamaCommand.Source
} else {
    $WingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $LlamaExecutable = Get-ChildItem -Path $WingetPackages -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $LlamaExecutable) {
        throw "Could not find $LlamaCommand on PATH or under WinGet packages. Install llama.cpp or reopen PowerShell after winget install."
    }
}

if ($LlamaCommand -like "*llama-server*") {
    $ServerArgs = @(
        "--model", $ModelPath,
        "--host", "127.0.0.1",
        "--port", "8080",
        "--ctx-size", "8192",
        "--n-gpu-layers", "99"
    )
} else {
    $ServerArgs = @(
        "serve",
        "--model", $ModelPath,
        "--host", "127.0.0.1",
        "--port", "8080",
        "--ctx-size", "8192",
        "--n-gpu-layers", "99"
    )
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
    Write-Host "Logs: $StderrLog"
} else {
    & $LlamaExecutable @ServerArgs
}
