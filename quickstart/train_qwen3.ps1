param(
    [string]$Python = "",
    [string]$Character = "",
    [string]$Config = "training/qwen3/configs/characters/elysia.yaml",
    [string]$ResumeFrom = "",
    [switch]$FromScratch,
    [switch]$DryRun,
    [switch]$PrepareOnly,
    [switch]$ForcePrepare,
    [switch]$SkipNormalize,
    [switch]$SkipSplit,
    [switch]$SkipValidate,
    [switch]$SkipConvert
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")

function Invoke-TrainingPython {
    param([string[]]$Arguments)
    & $RuntimePython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "python command failed: $RuntimePython $($Arguments -join ' ')"
    }
}

try {
    $RuntimePython = Resolve-AlysiaRuntimePython -PythonOverride $Python
} catch {
    throw "Qwen3 training requires quickstart/bootstrap.ps1 first. $($_.Exception.Message)"
}

Write-Host "Using unified runtime Python: $RuntimePython"
if (-not [string]::IsNullOrWhiteSpace($Character)) {
    $Config = "training/qwen3/configs/characters/$Character.yaml"
}
Write-Host "Using Qwen3 training config: $Config"

if ($ForcePrepare) {
    Write-Host "Dataset preparation will be forced."
}

$StatusJson = & $RuntimePython (Join-Path $ProjectRoot "training\qwen3\preprocess\status.py") --config $Config
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect Qwen3 preprocessing status."
}
$PreprocessStatus = $StatusJson | ConvertFrom-Json

if ((-not $SkipNormalize) -and ($ForcePrepare -or (-not $PreprocessStatus.normalized_current))) {
    Write-Host ""
    Write-Host "Step 1/4: normalize dataset"
    Invoke-TrainingPython -Arguments @(
        (Join-Path $ProjectRoot "training\qwen3\preprocess\normalize.py"),
        "--config", $Config
    )
} elseif (-not $SkipNormalize) {
    Write-Host ""
    Write-Host "Step 1/4: normalize dataset already current"
}

if ((-not $SkipSplit) -and ($ForcePrepare -or (-not $PreprocessStatus.split_current))) {
    Write-Host ""
    Write-Host "Step 2/4: split dataset"
    Invoke-TrainingPython -Arguments @(
        (Join-Path $ProjectRoot "training\qwen3\preprocess\split.py"),
        "--config", $Config
    )
} elseif (-not $SkipSplit) {
    Write-Host ""
    Write-Host "Step 2/4: split dataset already current"
}

if (-not $SkipValidate) {
    Write-Host ""
    Write-Host "Step 3/4: validate dataset"
    Invoke-TrainingPython -Arguments @(
        (Join-Path $ProjectRoot "training\qwen3\preprocess\validate.py"),
        "--config", $Config
    )
}

if ($PrepareOnly) {
    Write-Host ""
    Write-Host "Dataset preparation complete. Training was skipped because -PrepareOnly was provided."
    exit 0
}

Write-Host ""
Write-Host "Step 4/4: train Qwen3 LoRA"
$TrainArgs = @(
    (Join-Path $ProjectRoot "training\qwen3\train\train_lora.py"),
    "--config", $Config
)
if (-not [string]::IsNullOrWhiteSpace($ResumeFrom)) {
    $TrainArgs += @("--resume-from", $ResumeFrom)
}
if ($FromScratch) {
    $TrainArgs += "--from-scratch"
}
if ($DryRun) {
    $TrainArgs += "--dry-run"
}

try {
    Invoke-TrainingPython -Arguments $TrainArgs
} catch {
    Write-Host ""
    Write-Host "If this is a real QLoRA run, make sure these were completed first:"
    Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/install_qwen3_training_deps.ps1"
    Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/download_qwen3_training_model.ps1"
    throw
}

if ((-not $DryRun) -and (-not $SkipConvert)) {
    Write-Host ""
    Write-Host "Step 5/5: convert and register Qwen3 LoRA GGUF"
    $ConvertArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $ProjectRoot "quickstart\convert_qwen3_lora_to_gguf.ps1"),
        "-Python", $RuntimePython,
        "-Config", $Config
    )
    & powershell @ConvertArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Qwen3 LoRA GGUF conversion failed."
    }
}
