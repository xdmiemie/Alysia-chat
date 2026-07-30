param(
    [string]$Character = "elysia",
    [string]$Qwen3Model = "",
    [string[]]$AllowUserId = @(),
    [string]$Config = "configs/runtime/wechat.yaml",
    [string]$Python = "",
    [switch]$Qwen3Interactive,
    [switch]$ForceLogin,
    [switch]$SkipLogin,
    [switch]$SkipQwen3,
    [switch]$SkipBootstrap,
    [switch]$LightweightBootstrap,
    [switch]$SkipLfsPull,
    [switch]$SkipThirdParty,
    [switch]$SkipModelDownloads,
    [switch]$KeepQwen3,
    [switch]$Once,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Join-ProjectPath {
    param([string]$Path)
    if ([IO.Path]::IsPathRooted($Path)) {
        return $Path
    }
    return (Join-Path $ProjectRoot $Path)
}

function Get-WeChatCredentialsPath {
    param([string]$ConfigPath)
    $ResolvedConfig = Join-ProjectPath $ConfigPath
    if (-not (Test-Path -LiteralPath $ResolvedConfig)) {
        return (Join-ProjectPath "storage/secrets/ilink/account.json")
    }

    foreach ($Line in Get-Content -LiteralPath $ResolvedConfig -Encoding UTF8) {
        $Trimmed = ($Line -split "#", 2)[0].Trim()
        if ($Trimmed -like "credentials_path:*") {
            $Value = $Trimmed.Substring("credentials_path:".Length).Trim().Trim("'").Trim('"')
            if (-not [string]::IsNullOrWhiteSpace($Value)) {
                return (Join-ProjectPath $Value)
            }
        }
    }
    return (Join-ProjectPath "storage/secrets/ilink/account.json")
}

function Update-WeChatAllowList {
    param(
        [string]$ConfigPath,
        [string[]]$UserIds
    )

    if ($UserIds.Count -eq 0) {
        return
    }

    $ResolvedConfig = Join-ProjectPath $ConfigPath
    if (-not (Test-Path -LiteralPath $ResolvedConfig)) {
        throw "WeChat config not found: $ResolvedConfig"
    }

    $Lines = [System.Collections.Generic.List[string]]::new()
    $Lines.AddRange([string[]](Get-Content -LiteralPath $ResolvedConfig -Encoding UTF8))

    $AllowIndex = -1
    for ($Index = 0; $Index -lt $Lines.Count; $Index++) {
        if ($Lines[$Index].Trim() -eq "allow_user_ids:") {
            $AllowIndex = $Index
            break
        }
    }
    if ($AllowIndex -lt 0) {
        throw "Could not find contacts.allow_user_ids in $ResolvedConfig"
    }

    $Existing = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $InsertIndex = $AllowIndex + 1
    while ($InsertIndex -lt $Lines.Count) {
        $Line = $Lines[$InsertIndex]
        $Trimmed = $Line.Trim()
        if ($Trimmed.StartsWith("- ")) {
            [void]$Existing.Add($Trimmed.Substring(2).Trim())
            $InsertIndex++
            continue
        }
        if ($Trimmed.Length -eq 0) {
            $InsertIndex++
            continue
        }
        break
    }

    foreach ($UserId in $UserIds) {
        $CleanUserId = $UserId.Trim()
        if ([string]::IsNullOrWhiteSpace($CleanUserId)) {
            continue
        }
        if (-not $Existing.Contains($CleanUserId)) {
            $Lines.Insert($InsertIndex, "    - $CleanUserId")
            $InsertIndex++
            [void]$Existing.Add($CleanUserId)
            Write-Host "Added WeChat allowlist user: $CleanUserId"
        }
    }

    Set-Content -LiteralPath $ResolvedConfig -Value $Lines -Encoding UTF8
}

function Get-Qwen3Pid {
    $PidFile = Join-ProjectPath "storage/temporary/qwen3-llama-server.pid"
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return 0
    }
    try {
        $ProcessId = [int](Get-Content -LiteralPath $PidFile)
        $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($Process) {
            return $ProcessId
        }
    } catch {
        return 0
    }
    return 0
}

function Stop-Qwen3StartedByQuickstart {
    param([int]$ProcessId)
    if ($ProcessId -le 0) {
        return
    }
    try {
        Stop-Process -Id $ProcessId -ErrorAction Stop
        Write-Host "Stopped Qwen3 llama-server process $ProcessId."
    } catch {
        $ExistingProcess = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            Write-Host "Could not stop Qwen3 llama-server process ${ProcessId}: $($_.Exception.Message)"
        } else {
            Write-Host "Qwen3 llama-server process $ProcessId was already stopped."
        }
    }
    $PidFile = Join-ProjectPath "storage/temporary/qwen3-llama-server.pid"
    if (Test-Path -LiteralPath $PidFile) {
        $RecordedPid = [int](Get-Content -LiteralPath $PidFile)
        if ($RecordedPid -eq $ProcessId) {
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not $SkipBootstrap) {
    $BootstrapArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($Python)) {
        $BootstrapArgs += @("-Python", $Python)
    }
    if ($LightweightBootstrap) {
        $BootstrapArgs += "-Lightweight"
    }
    if ($SkipLfsPull) {
        $BootstrapArgs += "-SkipLfsPull"
    }
    if ($SkipThirdParty) {
        $BootstrapArgs += "-SkipThirdParty"
    }
    if ($SkipModelDownloads) {
        $BootstrapArgs += "-SkipModelDownloads"
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "quickstart\bootstrap.ps1") @BootstrapArgs
}

Update-WeChatAllowList -ConfigPath $Config -UserIds $AllowUserId

$CredentialsPath = Get-WeChatCredentialsPath -ConfigPath $Config
if (-not $SkipLogin) {
    if ($ForceLogin -or -not (Test-Path -LiteralPath $CredentialsPath)) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\ilink_login.ps1") -CredentialsPath ($CredentialsPath.Substring($ProjectRoot.Length).TrimStart("\", "/"))
    } else {
        Write-Host "Reusing iLink credentials: $CredentialsPath"
    }
}

$ExistingQwen3Pid = Get-Qwen3Pid
$StartedQwen3Pid = 0
$BridgeExitCode = 0

try {
if (-not $SkipQwen3) {
    $QwenArgs = @("-LlamaServer", "-Character", $Character)
    if (-not [string]::IsNullOrWhiteSpace($Qwen3Model)) {
        $QwenArgs += @("-Qwen3Model", $Qwen3Model)
    }
    if ($Qwen3Interactive) {
        $QwenArgs += "-Qwen3Interactive"
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\start_runtime.ps1") @QwenArgs
    $CurrentQwen3Pid = Get-Qwen3Pid
    if ($ExistingQwen3Pid -le 0 -and $CurrentQwen3Pid -gt 0) {
        $StartedQwen3Pid = $CurrentQwen3Pid
    }
}

$BridgeArgs = @("-Character", $Character, "-Config", $Config)
if ($Once) {
    $BridgeArgs += "-Once"
}
if ($Json) {
    $BridgeArgs += "-Json"
}

Write-Host "Starting Stage 11 iLink WeChat text bridge..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\start_wechat.ps1") @BridgeArgs
$BridgeExitCode = $LASTEXITCODE
} finally {
    if ($StartedQwen3Pid -gt 0 -and -not $KeepQwen3) {
        Stop-Qwen3StartedByQuickstart -ProcessId $StartedQwen3Pid
    } elseif ($StartedQwen3Pid -gt 0 -and $KeepQwen3) {
        Write-Host "Leaving Qwen3 llama-server running with PID $StartedQwen3Pid because -KeepQwen3 was set."
    }
}

if ($BridgeExitCode -ne 0) {
    exit $BridgeExitCode
}
