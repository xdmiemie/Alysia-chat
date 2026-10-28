Param(
    [string]$Python = "",
    [string]$Repo = "",
    [string]$Config = "",
    [string]$Character = "",
    [string]$Model = "",
    [string]$BindAddr = "127.0.0.1",
    [int]$Port = 9880
)

$ErrorActionPreference = "Stop"
$env:PYTHONUNBUFFERED = "1"

function Resolve-ProjectPath {
    param([string]$PathValue, [string]$BasePath)
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return (Resolve-Path -LiteralPath $PathValue).Path
    }
    return (Resolve-Path -LiteralPath (Join-Path $BasePath $PathValue)).Path
}

function Convert-TtsConfigToResolvedPaths {
    param(
        [string]$SourceConfig,
        [string]$OutputConfig,
        [string]$ProjectRoot,
        [string]$GptSoVitsRepo
    )

    $Lines = Get-Content -LiteralPath $SourceConfig
    $Converted = foreach ($Line in $Lines) {
        if ($Line -match '^(\s+[A-Za-z0-9_]+_path:\s*)(.+?)\s*$') {
            $Prefix = $Matches[1]
            $Value = $Matches[2].Trim().Trim("'").Trim('"')
            if ([System.IO.Path]::IsPathRooted($Value)) {
                "${Prefix}$Value"
            } else {
                $BasePath = $ProjectRoot
                if ($Value -like "GPT_SoVITS/*" -or $Value -like "GPT_SoVITS\*" -or $Value -like "tools/*" -or $Value -like "tools\*") {
                    $BasePath = $GptSoVitsRepo
                }
                $Resolved = [System.IO.Path]::GetFullPath((Join-Path $BasePath $Value)).Replace("\", "/")
                "${Prefix}$Resolved"
            }
        } else {
            $Line
        }
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $OutputConfig) | Out-Null
    $Converted | Set-Content -LiteralPath $OutputConfig -Encoding UTF8
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
. (Join-Path $ProjectRoot "scripts\gpt_sovits_requirements.ps1")
$CompatPath = Join-Path $ProjectRoot "runtime\compat"
if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $CompatPath
} else {
    $env:PYTHONPATH = "$CompatPath;$env:PYTHONPATH"
}
$ProjectFfmpegBin = Join-Path $ProjectRoot "tools\ffmpeg\bin"
$ProjectFfmpeg = Join-Path $ProjectFfmpegBin "ffmpeg.exe"
if (-not (Test-Path -LiteralPath $ProjectFfmpeg)) {
    throw "Project FFmpeg not found at $ProjectFfmpeg. Run quickstart/bootstrap.ps1 first."
}
$env:Path = "$ProjectFfmpegBin;$env:Path"
if ([string]::IsNullOrWhiteSpace($Repo)) {
    $Repo = Join-Path $ProjectRoot "third_party\GPT-SoVITS"
}
if ([string]::IsNullOrWhiteSpace($Character)) {
    $Character = "elysia"
}
if ([string]::IsNullOrWhiteSpace($Config)) {
    $GeneratedConfig = Join-Path $ProjectRoot "storage\temporary\gpt_sovits_tts_infer_$Character.active.yaml"
    $InferConfigArgs = @(
        (Join-Path $ProjectRoot "scripts\gpt_sovits_models.py"),
        "write-infer-config",
        "--character", $Character,
        "--output", $GeneratedConfig
    )
    if (-not [string]::IsNullOrWhiteSpace($Model)) {
        $InferConfigArgs += @("--model", $Model)
    }
    & (Resolve-AstrbotChatRuntimePython -PythonOverride $Python) @InferConfigArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Could not generate GPT-SoVITS upstream infer config from active model."
    }
    $Config = $GeneratedConfig
}

$Repo = Resolve-ProjectPath -PathValue $Repo -BasePath $ProjectRoot
$Config = Resolve-ProjectPath -PathValue $Config -BasePath $ProjectRoot
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\patch_gpt_sovits_windows.ps1") -Repo $Repo

$ResolvedConfig = Join-Path $ProjectRoot "storage\temporary\gpt_sovits_tts_infer_$Character.resolved.yaml"
Convert-TtsConfigToResolvedPaths -SourceConfig $Config -OutputConfig $ResolvedConfig -ProjectRoot $ProjectRoot -GptSoVitsRepo $Repo

$ResolvedPython = Resolve-AstrbotChatRuntimePython -PythonOverride $Python

Set-Location $Repo
Write-Host "Using GPT-SoVITS Python: $ResolvedPython"
Write-Host "Using project FFmpeg: $ProjectFfmpeg"
Write-Host "Using GPT-SoVITS character: $Character"
if (-not [string]::IsNullOrWhiteSpace($Model)) {
    Write-Host "Using GPT-SoVITS model: $Model"
}
Write-Host "Using GPT-SoVITS config: $ResolvedConfig"
Set-Content -LiteralPath (Join-Path $ProjectRoot "storage\temporary\gpt-sovits-upstream.character") -Value $Character -Encoding UTF8
Set-Content -LiteralPath (Join-Path $ProjectRoot "storage\temporary\gpt-sovits-upstream.model") -Value $Model -Encoding UTF8

$GptSoVitsRequirements = Join-Path $Repo "requirements.txt"
if (Test-Path -LiteralPath $GptSoVitsRequirements) {
    Write-Host "Ensuring GPT-SoVITS Python requirements are installed..."
    $PlatformGptSoVitsRequirements = New-GptSoVitsPlatformRequirements `
        -Source $GptSoVitsRequirements `
        -Destination (Join-Path $ProjectRoot "storage\temporary\gpt_sovits_requirements.resolved.txt")
    & $ResolvedPython -m pip install -r $PlatformGptSoVitsRequirements
    if ($LASTEXITCODE -ne 0) {
        throw "pip command failed: $ResolvedPython -m pip install -r $PlatformGptSoVitsRequirements"
    }
    & $ResolvedPython -m pip install opencc-python-reimplemented
    if ($LASTEXITCODE -ne 0) {
        throw "pip command failed: $ResolvedPython -m pip install opencc-python-reimplemented"
    }
}

& $ResolvedPython -u api_v2.py -a $BindAddr -p $Port -c $ResolvedConfig
