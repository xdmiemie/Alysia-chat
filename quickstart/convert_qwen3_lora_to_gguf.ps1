param(
    [string]$Python = "",
    [string]$Character = "",
    [string]$Config = "training/qwen3/configs/characters/elysia.yaml",
    [string]$ModelId = "",
    [string]$DisplayName = "",
    [string]$AdapterDir = "",
    [string]$LlamaCppSourceDir = "tools/llama.cpp-source",
    [switch]$Force,
    [switch]$Activate
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "command failed: $Command $($Arguments -join ' ')"
    }
}

function Invoke-Python {
    param([string[]]$Arguments)
    Invoke-Checked -Command $RuntimePython -Arguments $Arguments
}

function Invoke-DownloadFile {
    param([string]$Url, [string]$Destination, [int64]$MinBytes = 1)
    New-Item -ItemType Directory -Force -Path (Split-Path $Destination) | Out-Null
    $Partial = "$Destination.part"
    $Curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $Curl) {
        throw "curl.exe was not found; cannot download $Url"
    }
    & $Curl.Source --location --fail --retry 10 --retry-delay 2 --retry-all-errors --continue-at - --output $Partial $Url
    if ($LASTEXITCODE -ne 0) {
        throw "download failed; partial file kept for resume: $Partial"
    }
    $Downloaded = Get-Item -LiteralPath $Partial
    if ($Downloaded.Length -lt $MinBytes) {
        throw "downloaded file is too small: $Partial"
    }
    Move-Item -LiteralPath $Partial -Destination $Destination -Force
}

function Ensure-LlamaCppSource {
    param([string]$SourcePath)
    $ResolvedSource = Join-Path $ProjectRoot $SourcePath
    $Converter = Join-Path $ResolvedSource "convert_lora_to_gguf.py"
    if (Test-Path -LiteralPath $Converter) {
        return $ResolvedSource
    }

    Write-Host "Preparing llama.cpp source converter..."
    $DownloadRoot = Join-Path $ProjectRoot "storage\temporary\llama.cpp-source"
    $ZipPath = Join-Path $DownloadRoot "llama.cpp-master.zip"
    $ExtractRoot = Join-Path $DownloadRoot "extract"
    Invoke-DownloadFile `
        -Url "https://github.com/ggml-org/llama.cpp/archive/refs/heads/master.zip" `
        -Destination $ZipPath `
        -MinBytes 1000000

    if (Test-Path -LiteralPath $ExtractRoot) {
        Remove-Item -LiteralPath $ExtractRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $ExtractRoot | Out-Null
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractRoot -Force

    $Extracted = Get-ChildItem -LiteralPath $ExtractRoot -Directory |
        Where-Object { $_.Name -like "llama.cpp-*" } |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $Extracted) {
        throw "downloaded llama.cpp source archive did not contain the expected directory."
    }
    if (Test-Path -LiteralPath $ResolvedSource) {
        Remove-Item -LiteralPath $ResolvedSource -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $ResolvedSource) | Out-Null
    Move-Item -LiteralPath $Extracted -Destination $ResolvedSource

    $Converter = Join-Path $ResolvedSource "convert_lora_to_gguf.py"
    if (-not (Test-Path -LiteralPath $Converter)) {
        throw "llama.cpp source is missing convert_lora_to_gguf.py"
    }
    return $ResolvedSource
}

try {
    $RuntimePython = Resolve-AlysiaQwenPython -PythonOverride $Python
} catch {
    throw "Qwen3 LoRA conversion requires quickstart/bootstrap.ps1 first. $($_.Exception.Message)"
}

if (-not [string]::IsNullOrWhiteSpace($Character)) {
    $Config = "training/qwen3/configs/characters/$Character.yaml"
}

$ConfigInfoJson = & $RuntimePython -c "import json, sys; from pathlib import Path; from training.qwen3.config import load_training_config, trained_model_identity; c=load_training_config(sys.argv[1]); model_id, display_name=trained_model_identity(c); print(json.dumps({'character_id': c.character_id, 'run_name': c.run_name, 'run_dir': str(c.run_dir), 'hf_path': str(c.base_model.hf_path) if c.base_model.hf_path else '', 'gguf_path': str(c.base_model.gguf_path), 'trained_model_id': model_id, 'trained_model_display_name': display_name}, ensure_ascii=False))" $Config
if ($LASTEXITCODE -ne 0) {
    throw "Could not read Qwen3 training config: $Config"
}
$ConfigInfo = $ConfigInfoJson | ConvertFrom-Json
$CharacterId = [string]$ConfigInfo.character_id
$RunName = [string]$ConfigInfo.run_name
$RunDir = [string]$ConfigInfo.run_dir
$HfPath = [string]$ConfigInfo.hf_path

if ([string]::IsNullOrWhiteSpace($AdapterDir)) {
    $AdapterDir = Join-Path $RunDir "adapter-final"
}
if ([string]::IsNullOrWhiteSpace($ModelId)) {
    $ModelId = [string]$ConfigInfo.trained_model_id
}
if ([string]::IsNullOrWhiteSpace($DisplayName)) {
    $DisplayName = [string]$ConfigInfo.trained_model_display_name
}
if ([string]::IsNullOrWhiteSpace($HfPath) -or (-not (Test-Path -LiteralPath $HfPath))) {
    throw "Qwen3 HF model is missing. Run quickstart/download_qwen3_training_model.ps1 first."
}
if (-not (Test-Path -LiteralPath (Join-Path $AdapterDir "adapter_model.safetensors"))) {
    throw "PEFT LoRA adapter not found: $(Join-Path $AdapterDir 'adapter_model.safetensors')"
}

$OutputDir = Join-Path $ProjectRoot "model_store\registry\qwen3\$CharacterId\$ModelId"
$OutputFile = Join-Path $OutputDir "adapter_model.gguf"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
if ((Test-Path -LiteralPath $OutputFile) -and $Force) {
    throw "Model version already exists; refusing to overwrite weights: $OutputFile. Use a new training run name."
}

if ((Test-Path -LiteralPath $OutputFile) -and (-not $Force)) {
    Write-Host "LoRA GGUF already exists: $OutputFile"
} else {
    $SourceRoot = Ensure-LlamaCppSource -SourcePath $LlamaCppSourceDir
    $GgufPy = Join-Path $SourceRoot "gguf-py"
    $PreviousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "$SourceRoot;$GgufPy;$PreviousPythonPath"
    try {
        Invoke-Python -Arguments @(
            "-c",
            "import numpy, safetensors, sentencepiece, torch, transformers; import gguf"
        )

        $Converter = Join-Path $SourceRoot "convert_lora_to_gguf.py"
        Write-Host "Converting PEFT LoRA to GGUF..."
        Invoke-Python -Arguments @(
            $Converter,
            $AdapterDir,
            "--base", $HfPath,
            "--outfile", $OutputFile,
            "--outtype", "f16"
        )
    } finally {
        $env:PYTHONPATH = $PreviousPythonPath
    }
}

$ExportArgs = @(
    (Join-Path $ProjectRoot "training\qwen3\export\export_gguf.py"),
    "--config", $Config,
    "--adapter-path", $OutputFile,
    "--model-id", $ModelId,
    "--display-name", $DisplayName
)
if ($Activate) {
    $ExportArgs += "--activate"
}
Invoke-Python -Arguments $ExportArgs
Write-Host "Qwen3 LoRA GGUF is ready: $OutputFile"
