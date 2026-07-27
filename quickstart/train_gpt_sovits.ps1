param(
    [string]$Python = "",
    [string]$Character = "",
    [string]$Config = "training/gpt_sovits/configs/characters/elysia.yaml",
    [string]$ResumeFrom = "",
    [switch]$FromScratch,
    [switch]$DryRun,
    [switch]$ValidateOnly,
    [switch]$ForcePrepare,
    [switch]$SkipS1,
    [switch]$SkipS2,
    [switch]$SkipReferences
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
    throw "GPT-SoVITS training requires quickstart/bootstrap.ps1 first. $($_.Exception.Message)"
}

if (-not [string]::IsNullOrWhiteSpace($Character)) {
    $Config = "training/gpt_sovits/configs/characters/$Character.yaml"
}

Write-Host "Using unified runtime Python: $RuntimePython"
Write-Host "Using GPT-SoVITS training config: $Config"

Write-Host ""
Write-Host "Step 1/4: prepare and validate GPT-SoVITS dataset"
$ValidateArgs = @(
    (Join-Path $ProjectRoot "training\gpt_sovits\preprocess\dataset_manifest.py"),
    "--config", $Config,
    "--prepare",
    "--json"
)
if ($ForcePrepare) {
    $ValidateArgs += "--force"
}
& $RuntimePython @ValidateArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Dataset is not ready yet. Put Stage 8 WAV files here:"
    if (-not [string]::IsNullOrWhiteSpace($Character)) {
        Write-Host "training/gpt_sovits/datasets/$Character/raw/"
    } else {
        Write-Host "training/gpt_sovits/datasets/elysia/raw/"
    }
    throw "GPT-SoVITS dataset validation failed."
}

if ($ValidateOnly) {
    Write-Host ""
    Write-Host "Dataset validation complete. Training was skipped because -ValidateOnly was provided."
    exit 0
}

if (-not $SkipS1) {
    Write-Host ""
    Write-Host "Step 2/4: train GPT-SoVITS S1"
    $S1Args = @(
        (Join-Path $ProjectRoot "training\gpt_sovits\train\train_s1.py"),
        "--config", $Config,
        "--skip-prepare"
    )
    if (-not [string]::IsNullOrWhiteSpace($ResumeFrom)) {
        $S1Args += @("--resume-from", $ResumeFrom)
    }
    if ($FromScratch) {
        $S1Args += "--from-scratch"
    }
    if ($DryRun) {
        $S1Args += "--dry-run"
    }
    Invoke-TrainingPython -Arguments $S1Args
}

if (-not $SkipS2) {
    Write-Host ""
    Write-Host "Step 3/4: train GPT-SoVITS S2"
    $S2Args = @(
        (Join-Path $ProjectRoot "training\gpt_sovits\train\train_s2.py"),
        "--config", $Config,
        "--skip-prepare"
    )
    if (-not [string]::IsNullOrWhiteSpace($ResumeFrom)) {
        $S2Args += @("--resume-from", $ResumeFrom)
    }
    if ($FromScratch) {
        $S2Args += "--from-scratch"
    }
    if ($DryRun) {
        $S2Args += "--dry-run"
    }
    Invoke-TrainingPython -Arguments $S2Args
}

if ((-not $SkipReferences) -and (-not $DryRun)) {
    Write-Host ""
    Write-Host "Step 4/4: select reference clips"
    Invoke-TrainingPython -Arguments @(
        (Join-Path $ProjectRoot "training\gpt_sovits\export\select_references.py"),
        "--config", $Config
    )
} else {
    Write-Host ""
    Write-Host "Step 4/4: reference selection skipped"
}
