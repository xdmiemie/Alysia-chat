param(
    [string]$Python = "",
    [string]$PipCacheDir = "model_store/cache/pip",
    [switch]$CheckOnly,
    [switch]$SkipUpstream
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
. (Join-Path $ProjectRoot "scripts\gpt_sovits_requirements.ps1")

$PipCachePath = Join-Path $ProjectRoot $PipCacheDir
$StageRequirementFile = Join-Path $ProjectRoot "training\gpt_sovits\requirements.txt"
$UpstreamRequirementFile = Join-Path $ProjectRoot "third_party\GPT-SoVITS\requirements.txt"
$ResolvedUpstreamRequirementFile = Join-Path $ProjectRoot "storage\temporary\gpt_sovits_training_requirements.resolved.txt"

function Invoke-Pip {
    param([string]$PythonExe, [string[]]$Arguments)
    & $PythonExe -m pip @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "pip command failed: $PythonExe -m pip $($Arguments -join ' ')"
    }
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

function Get-MissingStage8Modules {
    param([string]$PythonExe)
    $Modules = @(
        [pscustomobject]@{ Module = "funasr"; Label = "funasr" },
        [pscustomobject]@{ Module = "modelscope"; Label = "modelscope" },
        [pscustomobject]@{ Module = "soundfile"; Label = "soundfile" },
        [pscustomobject]@{ Module = "torch"; Label = "torch" },
        [pscustomobject]@{ Module = "torchaudio"; Label = "torchaudio" },
        [pscustomobject]@{ Module = "tensorboard"; Label = "tensorboard" }
    )
    $Missing = @()
    foreach ($Module in $Modules) {
        if (-not (Test-PythonImport -PythonExe $PythonExe -ModuleName $Module.Module)) {
            $Missing += $Module.Label
        }
    }
    return $Missing
}

try {
    $RuntimePython = Resolve-AlysiaRuntimePython -PythonOverride $Python
} catch {
    throw "GPT-SoVITS training dependencies require quickstart/bootstrap.ps1 first. $($_.Exception.Message)"
}

Write-Host "Using unified runtime Python: $RuntimePython"
New-Item -ItemType Directory -Force -Path $PipCachePath | Out-Null

$MissingModules = @(Get-MissingStage8Modules -PythonExe $RuntimePython)
$NeedUpstreamInstall = $false
if ((-not $SkipUpstream) -and (Test-Path -LiteralPath $UpstreamRequirementFile)) {
    $NeedUpstreamInstall = -not (Test-PythonImport -PythonExe $RuntimePython -ModuleName "librosa")
}

if (($MissingModules.Count -eq 0) -and (-not $NeedUpstreamInstall)) {
    Write-Host "GPT-SoVITS training dependencies are already installed."
    exit 0
}

if ($CheckOnly) {
    foreach ($Module in $MissingModules) {
        Write-Host "[MISSING] $Module"
    }
    if ($NeedUpstreamInstall) {
        Write-Host "[MISSING] GPT-SoVITS upstream training requirements"
    }
    exit 1
}

Invoke-PipInstall -PythonExe $RuntimePython -Arguments @("--upgrade", "pip", "wheel", "setuptools<82")

if ($MissingModules.Count -gt 0) {
    if (-not (Test-Path -LiteralPath $StageRequirementFile)) {
        throw "Requirement file not found: $StageRequirementFile"
    }
    Write-Host "Installing Stage 8 GPT-SoVITS training requirements"
    Invoke-PipInstall -PythonExe $RuntimePython -Arguments @("-r", $StageRequirementFile)
}

if ($NeedUpstreamInstall) {
    $Resolved = New-GptSoVitsPlatformRequirements `
        -Source $UpstreamRequirementFile `
        -Destination $ResolvedUpstreamRequirementFile
    Write-Host "Installing GPT-SoVITS upstream training requirements from $Resolved"
    Invoke-PipInstall -PythonExe $RuntimePython -Arguments @("-r", $Resolved)
}

$MissingModules = @(Get-MissingStage8Modules -PythonExe $RuntimePython)
if ($MissingModules.Count -gt 0) {
    throw "GPT-SoVITS training dependencies are still missing: $($MissingModules -join ', ')"
}

Write-Host "GPT-SoVITS training dependencies are ready."
