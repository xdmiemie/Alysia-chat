param(
    [switch]$StartNarrator
)

$ErrorActionPreference = "Stop"

Write-Host "Stage 11 WeChat accessibility preparation"
Write-Host ""
Write-Host "Windows WeChat 4.x may expose only an MMUIRenderSubWindowHW render surface."
Write-Host "When that happens, pyweixin cannot read chat text from UI Automation."
Write-Host "Recent pywechat notes say Narrator may no longer restore the UI tree for affected accounts/versions."
Write-Host ""
Write-Host "Try this manual sequence first:"
Write-Host "1. Completely exit WeChat from the tray/taskbar."
Write-Host "2. Start Windows Narrator before logging in to WeChat."
Write-Host "3. Open WeChat and log in again."
Write-Host "4. Open the test chat 'n' and keep it visible."
Write-Host "5. Run scripts/dump_wechat_ui.ps1 and confirm it writes many UI rows, not just 1-2 rows."
Write-Host ""
Write-Host "If it still writes only 1-2 rows, this WeChat account/version is not UIA-readable here."
Write-Host "Next viable options are: use another UIA-readable WeChat account/environment, install a compatible WeChat version, or add an OCR-based adapter."
Write-Host ""

if ($StartNarrator) {
    Start-Process -FilePath "$env:WINDIR\system32\Narrator.exe"
    Write-Host "Started Windows Narrator."
} else {
    Write-Host "Run with -StartNarrator to launch Narrator from this script."
}
