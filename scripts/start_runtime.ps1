param(
    [switch]$AudioLoopback,
    [switch]$SenseVoice,
    [switch]$Doctor
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$CondaPrefixPython = Join-Path $ProjectRoot ".venv\python.exe"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (Test-Path $CondaPrefixPython) {
    $Python = $CondaPrefixPython
} elseif (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = "python"
}

if ($Doctor -or (-not $AudioLoopback -and -not $SenseVoice)) {
    & $Python (Join-Path $ProjectRoot "scripts\doctor.py")
}

if ($AudioLoopback) {
    & $Python (Join-Path $ProjectRoot "scripts\audio_loopback.py")
}

if ($SenseVoice) {
    & $Python -m uvicorn runtime.sensevoice.api:app --host 127.0.0.1 --port 5001
}
