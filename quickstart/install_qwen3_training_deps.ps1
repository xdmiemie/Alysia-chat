param(
    [string]$Python = "",
    [string]$PipCacheDir = "model_store/cache/pip",
    [string]$WheelhouseDir = "model_store/cache/qwen3_training_wheels",
    [switch]$CheckOnly,
    [switch]$DownloadOnly,
    [switch]$SkipTorch
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")

$PytorchCudaIndexUrl = "https://download.pytorch.org/whl/cu128"
$RequirementFile = Join-Path $ProjectRoot "training\qwen3\requirements.txt"
$PipCachePath = Join-Path $ProjectRoot $PipCacheDir
$TrainingWheelhouse = Join-Path $ProjectRoot $WheelhouseDir
$TorchWheelhouse = Join-Path $TrainingWheelhouse "torch-cu128"

function Invoke-Pip {
    param([string]$PythonExe, [string[]]$Arguments)
    & $PythonExe -m pip @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "pip command failed: $PythonExe -m pip $($Arguments -join ' ')"
    }
}

function Invoke-PipDownload {
    param([string]$PythonExe, [string[]]$Arguments)
    Invoke-Pip -PythonExe $PythonExe -Arguments (@(
        "download",
        "--retries", "10",
        "--timeout", "60",
        "--cache-dir", $PipCachePath
    ) + $Arguments)
}

function Invoke-PipInstall {
    param([string]$PythonExe, [string[]]$Arguments)
    Invoke-Pip -PythonExe $PythonExe -Arguments (@(
        "install",
        "--retries", "10",
        "--timeout", "60",
        "--cache-dir", $PipCachePath
    ) + $Arguments)
}

function Test-PythonImport {
    param([string]$PythonExe, [string]$ModuleName)
    $Probe = "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('$ModuleName') else 1)"
    & $PythonExe -c $Probe *> $null
    return $LASTEXITCODE -eq 0
}

function Test-PythonRequirement {
    param(
        [string]$PythonExe,
        [string]$ModuleName,
        [string]$DistributionName,
        [string]$MinVersion
    )
    $Probe = @'
import importlib.metadata as metadata
import importlib.util as util
import sys

module_name, distribution_name, min_version = sys.argv[1:4]
if util.find_spec(module_name) is None:
    raise SystemExit(1)

def version_tuple(value):
    parts = []
    token = str()
    separators = (chr(46), chr(43), chr(45))
    for char in value:
        if char.isdigit():
            token += char
        elif char in separators:
            if token:
                parts.append(int(token))
                token = str()
        else:
            break
    if token:
        parts.append(int(token))
    return tuple(parts)

try:
    installed = metadata.version(distribution_name)
except metadata.PackageNotFoundError:
    raise SystemExit(1)

raise SystemExit(0 if version_tuple(installed) >= version_tuple(min_version) else 1)
'@
    & $PythonExe -c $Probe $ModuleName $DistributionName $MinVersion *> $null
    return $LASTEXITCODE -eq 0
}

function Test-CudaTorch {
    param([string]$PythonExe)
    $Probe = @"
try:
    import torch
    raise SystemExit(0 if torch.cuda.is_available() else 1)
except Exception:
    raise SystemExit(1)
"@
    & $PythonExe -c $Probe *> $null
    return $LASTEXITCODE -eq 0
}

function Get-MissingTrainingModules {
    param([string]$PythonExe)
    $Requirements = @(
        [pscustomobject]@{ Module = "transformers"; Distribution = "transformers"; MinVersion = "4.51.0" },
        [pscustomobject]@{ Module = "datasets"; Distribution = "datasets"; MinVersion = "2.20.0" },
        [pscustomobject]@{ Module = "peft"; Distribution = "peft"; MinVersion = "0.12.0" },
        [pscustomobject]@{ Module = "accelerate"; Distribution = "accelerate"; MinVersion = "0.33.0" },
        [pscustomobject]@{ Module = "bitsandbytes"; Distribution = "bitsandbytes"; MinVersion = "0.43.0" },
        [pscustomobject]@{ Module = "safetensors"; Distribution = "safetensors"; MinVersion = "0.4.0" },
        [pscustomobject]@{ Module = "sentencepiece"; Distribution = "sentencepiece"; MinVersion = "0.2.0" },
        [pscustomobject]@{ Module = "google.protobuf"; Distribution = "protobuf"; MinVersion = "6.31.1" }
    )
    $Missing = @()
    foreach ($Requirement in $Requirements) {
        if (-not (Test-PythonRequirement `
            -PythonExe $PythonExe `
            -ModuleName $Requirement.Module `
            -DistributionName $Requirement.Distribution `
            -MinVersion $Requirement.MinVersion)) {
            $Missing += "$($Requirement.Distribution)>=$($Requirement.MinVersion)"
        }
    }
    return $Missing
}

try {
    $RuntimePython = Resolve-AlysiaQwenPython -PythonOverride $Python
} catch {
    throw "Qwen3 training dependencies require quickstart/bootstrap.ps1 first. $($_.Exception.Message)"
}

if (-not (Test-Path -LiteralPath $RequirementFile)) {
    throw "Requirement file not found: $RequirementFile"
}

Write-Host "Using Qwen3 Python: $RuntimePython"
New-Item -ItemType Directory -Force -Path $PipCachePath | Out-Null
New-Item -ItemType Directory -Force -Path $TrainingWheelhouse | Out-Null
New-Item -ItemType Directory -Force -Path $TorchWheelhouse | Out-Null

$TorchReady = Test-CudaTorch -PythonExe $RuntimePython
$MissingModules = @(Get-MissingTrainingModules -PythonExe $RuntimePython)

if ($TorchReady -and $MissingModules.Count -eq 0) {
    Write-Host "Qwen3 training dependencies are already installed."
    exit 0
}

if ($CheckOnly) {
    if ($TorchReady) {
        Write-Host "[OK] CUDA torch"
    } else {
        Write-Host "[MISSING] CUDA torch"
    }
    foreach ($Module in $MissingModules) {
        Write-Host "[MISSING] $Module"
    }
    exit 1
}

Invoke-PipInstall -PythonExe $RuntimePython -Arguments @("--upgrade", "pip", "wheel", "setuptools<82")

if (-not $TorchReady) {
    if ($SkipTorch) {
        Write-Host "Skipping CUDA torch installation because -SkipTorch was provided."
    } else {
        Write-Host "Downloading CUDA PyTorch wheels to $TorchWheelhouse"
        Invoke-PipDownload -PythonExe $RuntimePython -Arguments @(
            "--dest", $TorchWheelhouse,
            "--index-url", $PytorchCudaIndexUrl,
            "torch",
            "torchaudio"
        )
        if ($DownloadOnly) {
            Write-Host "Downloaded CUDA PyTorch wheels. Skipping install because -DownloadOnly was provided."
        } else {
            Write-Host "Installing CUDA PyTorch runtime from local wheelhouse"
            Invoke-PipInstall -PythonExe $RuntimePython -Arguments @(
                "--upgrade",
                "--no-index",
                "--find-links", $TorchWheelhouse,
                "torch",
                "torchaudio"
            )
        }
    }
}

$MissingModules = @(Get-MissingTrainingModules -PythonExe $RuntimePython)
if ($MissingModules.Count -gt 0) {
    Write-Host "Downloading Qwen3 training dependency wheels to $TrainingWheelhouse"
    Invoke-PipDownload -PythonExe $RuntimePython -Arguments @(
        "--dest", $TrainingWheelhouse,
        "--no-deps",
        "-r", $RequirementFile
    )
    if ($DownloadOnly) {
        Write-Host "Downloaded Qwen3 training dependency wheels. Skipping install because -DownloadOnly was provided."
        exit 0
    }
    Write-Host "Installing Qwen3 training dependencies from local wheelhouse"
    Invoke-PipInstall -PythonExe $RuntimePython -Arguments @(
        "--upgrade",
        "--find-links", $TrainingWheelhouse,
        "-r", $RequirementFile
    )
}

if ($DownloadOnly) {
    Write-Host "Downloaded requested Qwen3 training wheels. Skipping install because -DownloadOnly was provided."
    exit 0
}

$TorchReady = Test-CudaTorch -PythonExe $RuntimePython
$MissingModules = @(Get-MissingTrainingModules -PythonExe $RuntimePython)
if ((-not $SkipTorch) -and (-not $TorchReady)) {
    throw "CUDA torch is still unavailable after installation."
}
if ($MissingModules.Count -gt 0) {
    throw "Qwen3 training dependencies are still missing: $($MissingModules -join ', ')"
}

Write-Host "Qwen3 training dependencies are ready."
