param(
    [string]$QQPath = "",
    [string]$Version = "v2.1.1",
    [switch]$SkipQQ
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$InstallRoot = Join-Path $ProjectRoot "third_party\SnowLuma\$Version"
$Node = Join-Path $InstallRoot "node.exe"
$EntryPoint = Join-Path $InstallRoot "index.mjs"
$Marker = Join-Path $ProjectRoot "storage\temporary\snowluma-owned.json"

function Test-LocalPort {
    param([int]$Port)
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Connect = $Client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $Connect.AsyncWaitHandle.WaitOne(500, $false)) { return $false }
        $Client.EndConnect($Connect)
        return $true
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

function Get-OwnedProcess {
    if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) { return $null }
    try {
        $Saved = Get-Content -LiteralPath $Marker -Raw | ConvertFrom-Json
        $Process = Get-Process -Id ([int]$Saved.pid) -ErrorAction Stop
        if ($Process.Path -ne [string]$Saved.executable) { return $null }
        $Difference = [Math]::Abs(($Process.StartTime.ToUniversalTime() - [DateTime]::Parse([string]$Saved.started_at_utc).ToUniversalTime()).TotalSeconds)
        if ($Difference -gt 3) { return $null }
        return $Process
    } catch {
        return $null
    }
}

if (-not ((Test-Path -LiteralPath $Node -PathType Leaf) -and (Test-Path -LiteralPath $EntryPoint -PathType Leaf))) {
    throw "SnowLuma is missing at $InstallRoot. Review the release EULA and PRIVACY files, then manually extract the official Windows x64 full ZIP into that directory. Automated deployment is restricted by SnowLuma EULA section 5.4."
}

if (-not $SkipQQ) {
    $QQRunning = @(Get-Process -Name QQ -ErrorAction SilentlyContinue).Count -gt 0
    if ($QQRunning) {
        Write-Host "QQ is already running."
    } else {
        if (-not $QQPath) {
            foreach ($Candidate in @("D:\QQ\QQ.exe", "$env:ProgramFiles\Tencent\QQ\QQ.exe", "${env:ProgramFiles(x86)}\Tencent\QQ\QQ.exe")) {
                if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
                    $QQPath = $Candidate
                    break
                }
            }
        }
        if (-not $QQPath -or -not (Test-Path -LiteralPath $QQPath -PathType Leaf)) {
            throw "QQ.exe was not found. Pass -QQPath with its installed path."
        }
        Start-Process -FilePath $QQPath -WorkingDirectory (Split-Path -Parent $QQPath) | Out-Null
        Write-Host "Started QQ. Sign in in its window if needed."
    }
}

$Owned = Get-OwnedProcess
if ($Owned) {
    Write-Host "Reusing project SnowLuma process $($Owned.Id). WebUI: http://127.0.0.1:5099/"
    return
}
if (Test-Path -LiteralPath $Marker) { Remove-Item -LiteralPath $Marker -Force }
if (Test-LocalPort -Port 5099) {
    Write-Host "Reusing the existing local SnowLuma WebUI on port 5099. OneBot authentication will be checked by the chat runner."
    return
}

# Disable its remote version check and bypass pending automatic updates for this launch.
$PreviousUpdateCheck = $env:SNOWLUMA_UPDATE_CHECK
$PreviousUpdateRoot = $env:SNOWLUMA_UPDATE_ROOT
$PreviousHookAutoLoad = $env:SNOWLUMA_HOOK_AUTOLOAD
try {
    $env:SNOWLUMA_UPDATE_CHECK = "0"
    $env:SNOWLUMA_UPDATE_ROOT = $InstallRoot
    $env:SNOWLUMA_HOOK_AUTOLOAD = "1"
    $Process = Start-Process -FilePath $Node -ArgumentList @("index.mjs") -WorkingDirectory $InstallRoot -WindowStyle Hidden -PassThru
} finally {
    $env:SNOWLUMA_UPDATE_CHECK = $PreviousUpdateCheck
    $env:SNOWLUMA_UPDATE_ROOT = $PreviousUpdateRoot
    $env:SNOWLUMA_HOOK_AUTOLOAD = $PreviousHookAutoLoad
}
$Record = [ordered]@{
    pid = $Process.Id
    executable = $Node
    started_at_utc = $Process.StartTime.ToUniversalTime().ToString("o")
    version = $Version
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Marker) | Out-Null
$Record | ConvertTo-Json | Set-Content -LiteralPath $Marker -Encoding UTF8
Write-Host "Started SnowLuma process $($Process.Id)."

$Deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $Deadline) {
    if (Test-LocalPort -Port 5099) {
        Write-Host "SnowLuma WebUI: http://127.0.0.1:5099/"
        return
    }
    if ($Process.HasExited) {
        throw "SnowLuma exited with code $($Process.ExitCode). Check its local logs and first-run setup."
    }
    Start-Sleep -Seconds 2
}
Write-Warning "SnowLuma WebUI is not ready yet. Check local logs and first-run setup at http://127.0.0.1:5099/."
