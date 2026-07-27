function New-GptSoVitsPlatformRequirements {
    param(
        [string]$Source,
        [string]$Destination,
        [switch]$IncludePyOpenJTalk,
        [switch]$UsePyOpenJTalkPrebuilt
    )

    if (-not (Test-Path -LiteralPath $Source)) {
        throw "GPT-SoVITS requirement file not found: $Source"
    }

    $IsWindowsPlatform = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
    $Skipped = @()
    $Filtered = foreach ($Line in Get-Content -LiteralPath $Source) {
        $Trimmed = $Line.Trim()
        if ($IsWindowsPlatform -and $Trimmed -match '^(?i:pyopenjtalk)(?:[<>=!~ ].*)?$') {
            if ($UsePyOpenJTalkPrebuilt) {
                $Skipped += "$Trimmed -> pyopenjtalk-prebuilt==0.3.0"
                "pyopenjtalk-prebuilt==0.3.0"
                continue
            }
            if (-not $IncludePyOpenJTalk) {
                $Skipped += $Trimmed
                continue
            }
        }
        if ($IsWindowsPlatform -and $Trimmed -match '^(?i:--no-binary\s*=\s*opencc|opencc)(?:[<>=!~ ].*)?$') {
            $Skipped += $Trimmed
            continue
        }
        if ($IsWindowsPlatform -and $Trimmed -match '^(?i:jieba_fast)(?:[<>=!~ ].*)?$') {
            $Skipped += $Trimmed
            continue
        }
        $Line
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $Destination) | Out-Null
    $Filtered | Set-Content -LiteralPath $Destination -Encoding UTF8

    if ($Skipped.Count -gt 0) {
        Write-Host "Skipping Windows build-hostile GPT-SoVITS requirement(s): $($Skipped -join ', ')"
        Write-Host "Japanese TTS/training requires pyopenjtalk; run quickstart/install_gpt_sovits_training_deps.ps1 if your data uses ja."
        Write-Host "jieba_fast is replaced by the project jieba compatibility shim on Windows."
    }

    return $Destination
}
