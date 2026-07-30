param(
    [string]$ApiBaseUrl = "https://ilinkai.weixin.qq.com",
    [int]$BotType = 3,
    [string]$AccountId = "main",
    [string]$CredentialsPath = "storage/secrets/ilink/account.json",
    [string]$CursorPath = "storage/wechat/state/get_updates_buf.txt",
    [int]$TimeoutSeconds = 480,
    [int]$PollIntervalSeconds = 1,
    [switch]$OpenBrowser,
    [switch]$Manual,
    [string]$BotToken = "",
    [string]$BotUserId = "",
    [string]$BaseUrl = "",
    [switch]$Logout
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Resolve-ProjectPath {
    param([string]$Path)
    if ([IO.Path]::IsPathRooted($Path)) {
        return $Path
    }
    return (Join-Path $ProjectRoot $Path)
}

function Invoke-ILinkJson {
    param(
        [ValidateSet("GET", "POST")]
        [string]$Method,
        [string]$Uri,
        [object]$Body = $null,
        [int]$TimeoutSec = 30
    )

    $Arguments = @{
        Method = $Method
        Uri = $Uri
        TimeoutSec = $TimeoutSec
        Headers = @{
            Accept = "application/json"
            "User-Agent" = "Alysia-chat/0.1.0"
        }
    }
    if ($null -ne $Body) {
        $Arguments.ContentType = "application/json"
        $Arguments.Body = ($Body | ConvertTo-Json -Depth 20 -Compress)
    }
    return Invoke-RestMethod @Arguments
}

function Get-PropertyValue {
    param(
        [object]$Object,
        [string[]]$Names
    )
    if ($null -eq $Object) {
        return $null
    }
    foreach ($Name in $Names) {
        if ($Object.PSObject.Properties.Name -contains $Name) {
            $Value = $Object.$Name
            if ($null -ne $Value -and -not [string]::IsNullOrWhiteSpace([string]$Value)) {
                return $Value
            }
        }
    }
    return $null
}

function Get-ResponseData {
    param([object]$Response)
    $Data = Get-PropertyValue -Object $Response -Names @("data", "Data")
    if ($null -ne $Data) {
        return $Data
    }
    return $Response
}

function Save-ILinkCredentials {
    param(
        [string]$Token,
        [string]$BotId,
        [string]$ServiceBaseUrl,
        [string]$Account,
        [string]$CredentialFile
    )

    if ([string]::IsNullOrWhiteSpace($Token)) {
        throw "iLink login did not return bot_token."
    }
    if ([string]::IsNullOrWhiteSpace($BotId)) {
        throw "iLink login did not return ilink_bot_id."
    }
    if ([string]::IsNullOrWhiteSpace($ServiceBaseUrl)) {
        throw "iLink login did not return baseurl."
    }

    New-Item -ItemType Directory -Force (Split-Path -Parent $CredentialFile) | Out-Null
    $Payload = [ordered]@{
        bot_token = $Token
        bot_user_id = $BotId
        base_url = $ServiceBaseUrl.TrimEnd("/")
        account_id = $Account
    }
    $Temp = "$CredentialFile.tmp"
    $Payload | ConvertTo-Json | Set-Content -LiteralPath $Temp -Encoding UTF8
    Move-Item -LiteralPath $Temp -Destination $CredentialFile -Force
}

function Clear-ILinkCursor {
    param([string]$CursorFile)
    if (Test-Path -LiteralPath $CursorFile) {
        Remove-Item -LiteralPath $CursorFile -Force
        Write-Host "Cleared iLink cursor: $CursorFile"
    }
}

$CredentialFile = Resolve-ProjectPath $CredentialsPath
$CursorFile = Resolve-ProjectPath $CursorPath

if ($Logout) {
    if (Test-Path -LiteralPath $CredentialFile) {
        Remove-Item -LiteralPath $CredentialFile -Force
    }
    Clear-ILinkCursor -CursorFile $CursorFile
    Write-Host "iLink local credentials removed."
    exit 0
}

if ($Manual) {
    if ([string]::IsNullOrWhiteSpace($BotToken)) {
        $SecureToken = Read-Host "iLink Bot Token" -AsSecureString
        $BotToken = [Runtime.InteropServices.Marshal]::PtrToStringUni(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
        )
    }
    if ([string]::IsNullOrWhiteSpace($BotUserId)) {
        $BotUserId = Read-Host "iLink Bot User ID"
    }
    if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
        $BaseUrl = Read-Host "iLink API Base URL"
    }

    Save-ILinkCredentials -Token $BotToken -BotId $BotUserId -ServiceBaseUrl $BaseUrl -Account $AccountId -CredentialFile $CredentialFile
    Clear-ILinkCursor -CursorFile $CursorFile
    Write-Host "iLink credentials saved to $CredentialsPath."
    exit 0
}

$ApiBaseUrl = $ApiBaseUrl.TrimEnd("/")
$QrcodeUri = "$ApiBaseUrl/ilink/bot/get_bot_qrcode?bot_type=$BotType"
Write-Host "Requesting iLink login QR code from $ApiBaseUrl ..."
$QrcodeResponse = Invoke-ILinkJson -Method POST -Uri $QrcodeUri -Body @{ local_token_list = @() }
$QrcodeData = Get-ResponseData -Response $QrcodeResponse
$Qrcode = [string](Get-PropertyValue -Object $QrcodeData -Names @("qrcode", "qr_code", "code"))
$QrcodeUrl = [string](Get-PropertyValue -Object $QrcodeData -Names @("qrcode_url", "qrcodeUrl", "url"))
$QrcodeImage = [string](Get-PropertyValue -Object $QrcodeData -Names @("qrcode_img_content", "qrcodeImage", "qrcode_image", "image"))

if ([string]::IsNullOrWhiteSpace($Qrcode)) {
    throw "iLink did not return a qrcode value."
}

if (-not [string]::IsNullOrWhiteSpace($QrcodeImage)) {
    Write-Host ""
    Write-Host "Open this QR image URL and scan it with mobile WeChat:"
    Write-Host $QrcodeImage
} elseif (-not [string]::IsNullOrWhiteSpace($QrcodeUrl)) {
    Write-Host ""
    Write-Host "Open this login URL and scan/confirm with mobile WeChat:"
    Write-Host $QrcodeUrl
} else {
    Write-Host ""
    Write-Host "iLink QR code payload:"
    Write-Host $Qrcode
}

if ($OpenBrowser) {
    $OpenTarget = if (-not [string]::IsNullOrWhiteSpace($QrcodeImage)) { $QrcodeImage } elseif (-not [string]::IsNullOrWhiteSpace($QrcodeUrl)) { $QrcodeUrl } else { "" }
    if (-not [string]::IsNullOrWhiteSpace($OpenTarget)) {
        Start-Process $OpenTarget
    }
}

Write-Host ""
Write-Host "Waiting for scan confirmation. Timeout: $TimeoutSeconds seconds."
$Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$StatusUriBase = "$ApiBaseUrl/ilink/bot/get_qrcode_status?qrcode=$([uri]::EscapeDataString($Qrcode))"

while ((Get-Date) -lt $Deadline) {
    Start-Sleep -Seconds $PollIntervalSeconds
    $StatusResponse = Invoke-ILinkJson -Method GET -Uri $StatusUriBase -TimeoutSec 30
    $StatusData = Get-ResponseData -Response $StatusResponse

    $Status = [string](Get-PropertyValue -Object $StatusData -Names @("status", "state", "qrcode_status"))
    if (-not [string]::IsNullOrWhiteSpace($Status)) {
        Write-Host "iLink QR status: $Status"
    }

    $Token = [string](Get-PropertyValue -Object $StatusData -Names @("bot_token", "token", "access_token"))
    $BotId = [string](Get-PropertyValue -Object $StatusData -Names @("ilink_bot_id", "bot_user_id", "bot_id", "account_id"))
    $ServiceBaseUrl = [string](Get-PropertyValue -Object $StatusData -Names @("baseurl", "base_url", "api_base_url"))

    if (-not [string]::IsNullOrWhiteSpace($Token)) {
        if ([string]::IsNullOrWhiteSpace($ServiceBaseUrl)) {
            $ServiceBaseUrl = $ApiBaseUrl
        }
        Save-ILinkCredentials -Token $Token -BotId $BotId -ServiceBaseUrl $ServiceBaseUrl -Account $AccountId -CredentialFile $CredentialFile
        Clear-ILinkCursor -CursorFile $CursorFile
        Write-Host "iLink login succeeded."
        Write-Host "Credentials saved to $CredentialsPath."
        Write-Host "Bot user id: $BotId"
        exit 0
    }

    if ($Status -match "expired|timeout|cancel|refuse|deny|failed|error") {
        throw "iLink QR login failed or expired with status: $Status"
    }
}

throw "Timed out waiting for iLink QR login confirmation."
