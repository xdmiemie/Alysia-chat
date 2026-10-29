param([switch]$NoModels, [switch]$WithTTS, [switch]$WithoutTTS)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $root 'runtime_env\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Missing $python" }
$args = @((Join-Path $PSScriptRoot 'supervisor.py'))
if ($NoModels) { $args += '--no-models' }
if ($WithTTS) { $args += '--with-tts' }
if ($WithoutTTS) { $args += '--without-tts' }
& $python @args
exit $LASTEXITCODE
