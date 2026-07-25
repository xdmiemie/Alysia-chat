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

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    if ($content.Contains($New)) {
        Write-Host "[OK] Already patched $Path"
    } elseif ($content.Contains($Old)) {
        [System.IO.File]::WriteAllText($Path, $content.Replace($Old, $New), $Utf8NoBom)
        Write-Host "[OK] Patched $Path"
    } else {
        Write-Host "[OK] Already patched $Path"
    }
}

function Replace-ChineseJiebaImport {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
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

    if ($content.Contains($newChinese)) {
        Write-Host "[OK] Already patched $Path"
        return
    }

    $pattern = "import jieba_fast\r?\nimport logging\r?\n\r?\njieba_fast\.setLogLevel\(logging\.CRITICAL\)\r?\nimport jieba_fast\.posseg as psg"
    $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $pattern, $newChinese, 1)
    if ($patched -eq $content) {
        throw "Could not patch jieba_fast fallback in $Path"
    }

    [System.IO.File]::WriteAllText($Path, $patched, $Utf8NoBom)
    Write-Host "[OK] Patched $Path"
}

function Replace-RefAudioLoad {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $newLoad = @"
        raw_audio_np, raw_sr = librosa.load(ref_audio_path, sr=None, mono=False)
        if raw_audio_np.ndim == 1:
            raw_audio_np = raw_audio_np[None, :]
        raw_audio = torch.from_numpy(np.ascontiguousarray(raw_audio_np)).to(self.configs.device).float()
"@

    if ($content.Contains($newLoad)) {
        Write-Host "[OK] Already patched $Path"
        return
    }

    $pattern = "        raw_audio, raw_sr = torchaudio\.load\(ref_audio_path\)\r?\n        raw_audio = raw_audio\.to\(self\.configs\.device\)\.float\(\)"
    $patched = [System.Text.RegularExpressions.Regex]::Replace($content, $pattern, $newLoad, 1)
    if ($patched -eq $content) {
        throw "Could not patch torchaudio reference load in $Path"
    }

    [System.IO.File]::WriteAllText($Path, $patched, $Utf8NoBom)
    Write-Host "[OK] Patched $Path"
}

$toneSandhi = Join-Path $Repo "GPT_SoVITS\text\tone_sandhi.py"
$chinese = Join-Path $Repo "GPT_SoVITS\text\chinese.py"
$chinese2 = Join-Path $Repo "GPT_SoVITS\text\chinese2.py"
$tts = Join-Path $Repo "GPT_SoVITS\TTS_infer_pack\TTS.py"

Replace-Text `
    -Path $toneSandhi `
    -Old "import jieba_fast as jieba" `
    -New "try:`n    import jieba_fast as jieba`nexcept ModuleNotFoundError:`n    import jieba"

Replace-ChineseJiebaImport -Path $chinese
Replace-ChineseJiebaImport -Path $chinese2

Replace-RefAudioLoad -Path $tts
