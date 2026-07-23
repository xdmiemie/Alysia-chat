Param(
    [string]$Text = "",
    [string]$Emotion = "gentle",
    [int]$StartupTimeoutSeconds = 900
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$UpstreamScript = Join-Path $ProjectRoot "scripts\start_gpt_sovits_upstream.ps1"
$LogsDir = Join-Path $ProjectRoot "storage\logs"
$OutLog = Join-Path $LogsDir "gpt-sovits-upstream.out.log"
$ErrLog = Join-Path $LogsDir "gpt-sovits-upstream.err.log"
$ClientPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ([string]::IsNullOrWhiteSpace($Text)) {
    $Text = -join ([char[]](36763, 33510, 21862, 65292, 20170, 22825, 20808, 20241, 24687, 19968, 19979, 12290))
}

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
Remove-Item -LiteralPath $OutLog,$ErrLog -Force -ErrorAction SilentlyContinue

$process = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $UpstreamScript) `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru `
    -WindowStyle Hidden

try {
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "GPT-SoVITS upstream exited early with code $($process.ExitCode). See $ErrLog"
        }

        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:9880/docs" -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 5
        }
    }

    if (-not $ready) {
        throw "GPT-SoVITS upstream did not become ready within $StartupTimeoutSeconds seconds. See $ErrLog"
    }

    & $ClientPython (Join-Path $ProjectRoot "scripts\gpt_sovits_speech.py") $Text --emotion $Emotion
    if ($LASTEXITCODE -ne 0) {
        throw "Stage 4 speech smoke failed with exit code $LASTEXITCODE"
    }

    & $ClientPython (Join-Path $ProjectRoot "scripts\gpt_sovits_acceptance.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Stage 4 acceptance failed with exit code $LASTEXITCODE"
    }
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
}
