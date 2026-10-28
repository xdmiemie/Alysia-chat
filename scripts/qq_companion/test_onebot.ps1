$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dir = Join-Path $root 'external\SnowLuma\v2.1.1\config'
$files = @(Get-ChildItem -LiteralPath $dir -Filter 'onebot_*.json' -File)
if ($files.Count -ne 1) { throw 'Expected one SnowLuma account config.' }
$config = Get-Content -LiteralPath $files[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
$server = @($config.networks.httpServers | Where-Object { $_.port -eq 3000 })[0]
if (-not $server) { throw 'SnowLuma OneBot HTTP server 3000 is absent.' }
$headers = @{}
if ($server.accessToken) { $headers['Authorization'] = 'Bearer ' + $server.accessToken }
try {
    $response = Invoke-RestMethod -Uri 'http://127.0.0.1:3000/get_login_info' -Method Post -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 5
    if ($response.retcode -ne 0) { throw 'get_login_info returned nonzero retcode' }
    Write-Host '[OK] SnowLuma OneBot get_login_info succeeded (account details suppressed).'
} catch {
    Write-Error 'OneBot probe failed. Confirm NTQQ is logged in and SnowLuma is attached.'
    exit 1
}
