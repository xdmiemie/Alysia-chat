param(
    [string]$Python = "",
    [string]$RepoId = "Qwen/Qwen3-14B",
    [string]$Revision = "",
    [string]$TargetDir = "model_store/cache/qwen3/hf/Qwen3-14B",
    [string]$HfCacheDir = "model_store/cache/huggingface",
    [string]$Config = "training/qwen3/configs/characters/elysia.yaml",
    [int]$MaxWorkers = 4,
    [switch]$ForceDownload,
    [switch]$LocalFilesOnly,
    [switch]$VerifyOnly,
    [switch]$SkipConfigUpdate
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")

try {
    $RuntimePython = Resolve-AstrbotChatRuntimePython -PythonOverride $Python
} catch {
    throw "Qwen3 HF training model download requires quickstart/bootstrap.ps1 first. $($_.Exception.Message)"
}

$Arguments = @(
    (Join-Path $ProjectRoot "scripts\download_qwen3_hf_training_model.py"),
    "--repo-id", $RepoId,
    "--target-dir", $TargetDir,
    "--hf-cache-dir", $HfCacheDir,
    "--config", $Config
)
if (-not [string]::IsNullOrWhiteSpace($Revision)) {
    $Arguments += @("--revision", $Revision)
}
if ($MaxWorkers -gt 0) {
    $Arguments += @("--max-workers", "$MaxWorkers")
}
if ($ForceDownload) {
    $Arguments += "--force-download"
}
if ($LocalFilesOnly) {
    $Arguments += "--local-files-only"
}
if ($VerifyOnly) {
    $Arguments += "--verify-only"
}
if ($SkipConfigUpdate) {
    $Arguments += "--skip-config-update"
}

& $RuntimePython @Arguments
if ($LASTEXITCODE -ne 0) {
    if ($VerifyOnly) {
        exit $LASTEXITCODE
    }
    throw "Qwen3 HF training model download failed."
}
