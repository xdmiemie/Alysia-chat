param([switch]$SkipBrowser)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$base = Join-Path $root 'external\Python312\python.exe'
$python = Join-Path $root 'envs\douyin\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & $base -m venv (Join-Path $root 'envs\douyin')
    if ($LASTEXITCODE) { throw 'Douyin venv creation failed' }
}
& $python -m pip install -r (Join-Path $root 'services\douyin_worker\requirements.txt')
if ($LASTEXITCODE) { throw 'Douyin dependency installation failed' }
if (-not $SkipBrowser) {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $root 'tools\playwright-browsers'
    & $python -m playwright install chromium
    if ($LASTEXITCODE) { throw 'Chromium installation failed' }
}
$privateProfile = Join-Path $root 'storage\qq_companion\browser_profile'
New-Item -ItemType Directory -Path $privateProfile -Force | Out-Null
$privateOwner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $privateProfile /inheritance:r /grant:r "${privateOwner}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE) { throw 'Could not restrict browser profile permissions' }
# Reset only descendants to inherit this private root. Applying directory-only
# grants recursively to files can leave an empty DACL on existing files.
foreach ($profileChild in @(Get-ChildItem -LiteralPath $privateProfile -Force)) {
    & icacls.exe $profileChild.FullName /reset /T /C | Out-Null
    if ($LASTEXITCODE) { throw 'Could not apply private profile inheritance' }
}
$privateToken = Join-Path $root 'storage\qq_companion\douyin_token'
if (Test-Path -LiteralPath $privateToken) {
    & icacls.exe $privateToken /inheritance:r /grant:r "${privateOwner}:F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
    if ($LASTEXITCODE) { throw 'Could not restrict local token permissions' }
}
