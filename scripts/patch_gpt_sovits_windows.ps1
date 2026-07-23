Param(
    [string]$Repo = ""
)

$ErrorActionPreference = "Stop"

if (-not $Repo) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $Repo = Join-Path $ProjectRoot "third_party\GPT-SoVITS"
}

function Replace-Text {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Old,
        [Parameter(Mandatory = $true)][string]$New
    )

    $content = Get-Content -LiteralPath $Path -Raw
    if ($content.Contains($Old)) {
        Set-Content -LiteralPath $Path -Value $content.Replace($Old, $New) -NoNewline
        Write-Host "[OK] Patched $Path"
    } else {
        Write-Host "[OK] Already patched $Path"
    }
}

$toneSandhi = Join-Path $Repo "GPT_SoVITS\text\tone_sandhi.py"
$chinese = Join-Path $Repo "GPT_SoVITS\text\chinese.py"
$chinese2 = Join-Path $Repo "GPT_SoVITS\text\chinese2.py"

Replace-Text `
    -Path $toneSandhi `
    -Old "import jieba_fast as jieba" `
    -New "try:`n    import jieba_fast as jieba`nexcept ModuleNotFoundError:`n    import jieba"

$oldChinese = @"
import jieba_fast
import logging

jieba_fast.setLogLevel(logging.CRITICAL)
import jieba_fast.posseg as psg
"@

$newChinese = @"
import logging

try:
    import jieba_fast as jieba
    import jieba_fast.posseg as psg
except ModuleNotFoundError:
    import jieba
    import jieba.posseg as psg

jieba.setLogLevel(logging.CRITICAL)
"@

Replace-Text -Path $chinese -Old $oldChinese -New $newChinese
Replace-Text -Path $chinese2 -Old $oldChinese -New $newChinese
