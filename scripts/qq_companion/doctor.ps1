$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
& (Join-Path $root 'runtime_env\python.exe') (Join-Path $PSScriptRoot 'doctor.py')
exit $LASTEXITCODE
