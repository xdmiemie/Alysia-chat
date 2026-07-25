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

$toneSandhi = Join-Path $Repo "GPT_SoVITS\text\tone_sandhi.py"
$chinese = Join-Path $Repo "GPT_SoVITS\text\chinese.py"
$chinese2 = Join-Path $Repo "GPT_SoVITS\text\chinese2.py"
$tts = Join-Path $Repo "GPT_SoVITS\TTS_infer_pack\TTS.py"

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

$oldTorchaudioLoad = @"
        raw_audio, raw_sr = torchaudio.load(ref_audio_path)
        raw_audio = raw_audio.to(self.configs.device).float()
"@

$newLibrosaLoad = @"
        raw_audio_np, raw_sr = librosa.load(ref_audio_path, sr=None, mono=False)
        if raw_audio_np.ndim == 1:
            raw_audio_np = raw_audio_np[None, :]
        raw_audio = torch.from_numpy(np.ascontiguousarray(raw_audio_np)).to(self.configs.device).float()
"@

Replace-Text -Path $tts -Old $oldTorchaudioLoad -New $newLibrosaLoad
