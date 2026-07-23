Param(
    [string]$Python = "C:\Users\MLTZ\miniconda3\envs\GPTSoVits\python.exe",
    [string]$Repo = "D:\Alysia-chat\third_party\GPT-SoVITS",
    [string]$Config = "D:\Alysia-chat\configs\runtime\gpt_sovits_tts_infer_elysia.yaml",
    [string]$BindAddr = "127.0.0.1",
    [int]$Port = 9880
)

$ErrorActionPreference = "Stop"
$env:PYTHONUNBUFFERED = "1"

Set-Location $Repo
& $Python -u api_v2.py -a $BindAddr -p $Port -c $Config
