function New-GptSoVitsPlatformRequirements {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source)) {
        throw "GPT-SoVITS requirement file not found: $Source"
    }

    $IsWindowsPlatform = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
    $Skipped = @()
    $Filtered = foreach ($Line in Get-Content -LiteralPath $Source) {
        $Trimmed = $Line.Trim()
        if ($IsWindowsPlatform -and $Trimmed -match '^(?i:pyopenjtalk)(?:[<>=!~ ].*)?$') {
            $Skipped += $Trimmed
            continue
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
        Write-Host "Japanese TTS requires pyopenjtalk; install Visual Studio Build Tools if you need it."
        Write-Host "jieba_fast is replaced by the project jieba compatibility shim on Windows."
    }

    return $Destination
}
