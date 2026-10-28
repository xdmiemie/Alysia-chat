param([switch]$SkipPip)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$source = Join-Path $root 'external\AstrBot'
$python312 = Join-Path $root 'external\Python312\python.exe'
$venv = Join-Path $root 'envs\astrbot\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $source)) {
    git clone --depth 1 --branch v4.29.3 https://github.com/AstrBotDevs/AstrBot.git $source
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot clone failed' }
}
$sha = (git -C $source rev-parse HEAD).Trim()
if ($sha -ne 'f61a7f42696da6df25dc7a249ec9b8d54ed9da18') { throw "Unexpected AstrBot SHA: $sha" }
if (-not (Test-Path -LiteralPath $python312)) {
    throw 'Python 3.12.10 missing. Install official python-3.12.10-amd64.exe into external/Python312 first.'
}
if (-not (Test-Path -LiteralPath $venv)) {
    & $python312 -m venv (Join-Path $root 'envs\astrbot')
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot venv creation failed' }
}
if (-not $SkipPip) {
    & $venv -m pip install --disable-pip-version-check -e $source
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot dependency install failed' }
}
Push-Location $source
try {
    & $venv -m astrbot.cli init --yes
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot initialization failed' }
    & $venv -c "from astrbot.core.config.astrbot_config import AstrBotConfig; AstrBotConfig(config_path='data/cmd_config.json')"
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot default config creation failed' }
} finally {
    Pop-Location
}
Write-Host 'AstrBot v4.29.3 is installed in isolated envs/astrbot.'
Write-Host 'Run: runtime_env\python.exe scripts\qq_companion\configure.py --apply'
