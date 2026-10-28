param(
    [string]$Python = "",
    [switch]$Lightweight,
    [switch]$SkipThirdParty,
    [switch]$SkipModelDownloads,
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")
. (Join-Path $ProjectRoot "scripts\gpt_sovits_requirements.ps1")

$BundledPythonVersion = "3.10.11"
$BundledPythonUrl = "https://www.python.org/ftp/python/$BundledPythonVersion/python-$BundledPythonVersion-amd64.exe"
$FfmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$LlamaReleaseApiUrl = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
$LlamaFallbackReleaseTag = "b10099"
$LlamaFallbackAssetBaseUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaFallbackReleaseTag"
$GptSoVitsRepoUrl = "https://github.com/RVC-Boss/GPT-SoVITS.git"
$GptSoVitsPretrainedUrl = "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/pretrained_models.zip"
$GptSoVitsG2PwUrl = "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/G2PWModel.zip"
$PytorchCudaIndexUrl = "https://download.pytorch.org/whl/cu128"
$LlamaCudaRuntimeDlls = @(
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudart64_12.dll"
)

function Test-Python310 {
    param([string]$Command, [string[]]$Arguments = @())
    try {
        $ProbeArguments = @($Arguments) + @(
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        )
        $Output = & $Command @ProbeArguments 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
        $Version = ($Output | Select-Object -Last 1)
        return $Version -eq "3.10"
    } catch {
        return $false
    }
}

function Find-SystemPython310 {
    $Candidates = @(
        [pscustomobject]@{ Command = "py"; Arguments = @("-3.10") },
        [pscustomobject]@{ Command = "python"; Arguments = @() },
        [pscustomobject]@{ Command = "python3.10"; Arguments = @() },
        [pscustomobject]@{ Command = "python3"; Arguments = @() }
    )

    foreach ($Candidate in $Candidates) {
        if (-not (Get-Command $Candidate.Command -ErrorAction SilentlyContinue)) {
            continue
        }
        if (Test-Python310 -Command $Candidate.Command -Arguments $Candidate.Arguments) {
            return $Candidate
        }
    }

    throw "Could not find system Python 3.10. Bootstrap will download project-local Python $BundledPythonVersion."
}

function Install-ProjectPython310 {
    param([string]$InstallPath)

    $Candidate = Join-Path $InstallPath "python.exe"
    if ((Test-Path -LiteralPath $Candidate) -and (Test-Python310 -Command $Candidate)) {
        return [pscustomobject]@{ Command = $Candidate; Arguments = @() }
    }

    $DownloadDir = Join-Path $ProjectRoot "storage\temporary"
    New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
    $Installer = Join-Path $DownloadDir "python-$BundledPythonVersion-amd64.exe"

    if (-not (Test-Path -LiteralPath $Installer)) {
        Invoke-DownloadFile -Url $BundledPythonUrl -Destination $Installer -MinBytes 10000000
    }

    Write-Host "Installing project-local Python $BundledPythonVersion at '$InstallPath'"
    New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null
    $Arguments = "/quiet InstallAllUsers=0 TargetDir=`"$InstallPath`" Include_launcher=0 PrependPath=0 Include_pip=1 Include_test=0 Include_doc=0 Include_tcltk=0 SimpleInstall=1"
    $Process = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) {
        throw "Python $BundledPythonVersion installer failed with exit code $($Process.ExitCode)."
    }
    if (-not ((Test-Path -LiteralPath $Candidate) -and (Test-Python310 -Command $Candidate))) {
        throw "Installed project-local Python, but could not validate Python 3.10 at '$Candidate'."
    }
    return [pscustomobject]@{ Command = $Candidate; Arguments = @() }
}

function Resolve-OrInstallPython310 {
    $ProjectPython = Join-Path $ProjectRoot "runtime_python\python.exe"
    if ((Test-Path -LiteralPath $ProjectPython) -and (Test-Python310 -Command $ProjectPython)) {
        return [pscustomobject]@{ Command = $ProjectPython; Arguments = @() }
    }

    try {
        return Find-SystemPython310
    } catch {
        Write-Host $_.Exception.Message
        return Install-ProjectPython310 -InstallPath (Join-Path $ProjectRoot "runtime_python")
    }
}

function New-VenvRuntimeEnv {
    param([string]$EnvPath)
    $Python310 = Resolve-OrInstallPython310

    Write-Host "Creating project-local unified venv at '$EnvPath' with Python 3.10"
    $CreateArguments = @($Python310.Arguments) + @("-m", "venv", $EnvPath)
    & $Python310.Command @CreateArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create runtime_env with Python 3.10."
    }

    $Candidate = Join-Path $EnvPath "Scripts\python.exe"
    if (Test-Path -LiteralPath $Candidate) {
        return (Resolve-Path -LiteralPath $Candidate).Path
    }
    throw "Created venv at '$EnvPath', but could not resolve Scripts\python.exe."
}

function Resolve-OrCreateRuntimePython {
    param([string]$PythonOverride)
    try {
        return Resolve-AstrbotChatRuntimePython -PythonOverride $PythonOverride
    } catch {
        Write-Host $_.Exception.Message
        return New-VenvRuntimeEnv -EnvPath (Join-Path $ProjectRoot "runtime_env")
    }
}

function Install-Requirements {
    param([string]$Python, [string[]]$RequirementFiles)
    foreach ($Requirement in $RequirementFiles) {
        $Path = Join-Path $ProjectRoot $Requirement
        if (-not (Test-Path $Path)) {
            throw "Requirement file not found: $Path"
        }
        Write-Host "Installing $Requirement"
        Invoke-Pip -Python $Python -Arguments @("install", "-r", $Path)
    }
}

function Test-CudaTorch {
    param([string]$Python)
    $Probe = @"
try:
    import torch
    raise SystemExit(0 if torch.cuda.is_available() else 1)
except Exception:
    raise SystemExit(1)
"@
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python -c $Probe *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

function Install-CudaTorch {
    param([string]$Python)
    if (Test-CudaTorch -Python $Python) {
        Write-Host "CUDA PyTorch runtime is already available."
        return
    }

    Write-Host "Installing CUDA PyTorch runtime from $PytorchCudaIndexUrl"
    Invoke-Pip -Python $Python -Arguments @(
        "install",
        "--upgrade",
        "--force-reinstall",
        "--index-url", $PytorchCudaIndexUrl,
        "torch",
        "torchaudio"
    )

    if (-not (Test-CudaTorch -Python $Python)) {
        throw "CUDA PyTorch installation completed, but torch.cuda.is_available() is still false."
    }
}

function Invoke-Pip {
    param([string]$Python, [string[]]$Arguments)
    & $Python -m pip @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "pip command failed: $Python -m pip $($Arguments -join ' ')"
    }
}

function Invoke-RuntimePython {
    param([string]$Python, [string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "python command failed: $Python $($Arguments -join ' ')"
    }
}

function Invoke-Git {
    param([string[]]$Arguments)
    $Git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $Git) {
        throw "Could not find git. Install Git for Windows, then rerun quickstart/bootstrap.ps1."
    }

    & $Git.Source @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git command failed: git $($Arguments -join ' ')"
    }
}

function Install-GptSoVitsRepo {
    $RepoPath = Join-Path $ProjectRoot "third_party\GPT-SoVITS"
    $ApiPath = Join-Path $RepoPath "api_v2.py"
    if (Test-Path -LiteralPath $ApiPath) {
        Write-Host "Updating GPT-SoVITS upstream repo..."
        Invoke-Git -Arguments @("-C", $RepoPath, "pull", "--ff-only")
        return
    }

    if (Test-Path -LiteralPath $RepoPath) {
        $Children = Get-ChildItem -LiteralPath $RepoPath -Force | Where-Object { $_.Name -ne ".gitkeep" }
        if ($Children) {
            throw "third_party\GPT-SoVITS exists but is not a usable GPT-SoVITS repo. Move it aside or remove it, then rerun quickstart/bootstrap.ps1."
        }
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path $RepoPath) | Out-Null
    }

    Write-Host "Cloning GPT-SoVITS upstream repo..."
    if (Test-Path -LiteralPath $RepoPath) {
        Remove-Item -LiteralPath $RepoPath -Recurse -Force
    }
    Invoke-Git -Arguments @("clone", "--depth", "1", $GptSoVitsRepoUrl, $RepoPath)
}

function Invoke-DownloadFile {
    param(
        [string]$Url,
        [string]$Destination,
        [int64]$MinBytes = 1,
        [switch]$ValidateZip
    )

    if (Test-Path -LiteralPath $Destination) {
        $Existing = Get-Item -LiteralPath $Destination
        $ValidExisting = $Existing.Length -ge $MinBytes
        if ($ValidExisting -and $ValidateZip) {
            $ValidExisting = Test-ZipArchive -Path $Destination
        }
        if ($ValidExisting) {
            return
        }
        Write-Host "Discarding incomplete or invalid download: $Destination"
        Remove-Item -LiteralPath $Destination -Force
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $Destination) | Out-Null
    Write-Host "Downloading $Url"
    $PartialDestination = "$Destination.part"
    $Curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $Curl) {
        throw "Could not download $Url because curl.exe was not found."
    }
    $CurlArgs = @(
        "--location",
        "--fail",
        "--retry", "10",
        "--retry-delay", "2",
        "--retry-all-errors",
        "--continue-at", "-",
        "--output", $PartialDestination,
        $Url
    )
    & $Curl.Source @CurlArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Could not download $Url with curl.exe. Exit code: $LASTEXITCODE. Partial file kept for resume: $PartialDestination"
    }

    $Downloaded = Get-Item -LiteralPath $PartialDestination
    if ($Downloaded.Length -lt $MinBytes) {
        Remove-Item -LiteralPath $PartialDestination -Force
        throw "Downloaded file is too small: $Url ($($Downloaded.Length) bytes, expected at least $MinBytes)."
    }
    if ($ValidateZip -and -not (Test-ZipArchive -Path $PartialDestination)) {
        Remove-Item -LiteralPath $PartialDestination -Force
        throw "Downloaded zip archive is invalid or incomplete: $Url"
    }
    Move-Item -LiteralPath $PartialDestination -Destination $Destination -Force
}

function Test-ZipArchive {
    param([string]$Path)
    try {
        Add-Type -AssemblyName System.IO.Compression -ErrorAction SilentlyContinue
        $Stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        try {
            $Archive = [System.IO.Compression.ZipArchive]::new(
                $Stream,
                [System.IO.Compression.ZipArchiveMode]::Read
            )
            try {
                $null = $Archive.Entries.Count
                return $true
            } finally {
                $Archive.Dispose()
            }
        } finally {
            $Stream.Dispose()
        }
    } catch {
        return $false
    }
}

function Expand-ZipToDirectory {
    param([string]$ZipPath, [string]$Destination)
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $Destination -Force
}

function Install-ProjectFfmpeg {
    $ToolRoot = Join-Path $ProjectRoot "tools\ffmpeg"
    $FfmpegExe = Join-Path $ToolRoot "bin\ffmpeg.exe"
    if (Test-Path -LiteralPath $FfmpegExe) {
        return $FfmpegExe
    }

    $ZipPath = Join-Path $ProjectRoot "storage\temporary\ffmpeg-release-essentials.zip"
    $ExtractRoot = Join-Path $ProjectRoot "storage\temporary\ffmpeg-extract"
    Invoke-DownloadFile -Url $FfmpegUrl -Destination $ZipPath -MinBytes 80000000 -ValidateZip
    if (Test-Path -LiteralPath $ExtractRoot) {
        Remove-Item -LiteralPath $ExtractRoot -Recurse -Force
    }
    Expand-ZipToDirectory -ZipPath $ZipPath -Destination $ExtractRoot

    $ExtractedExe = Get-ChildItem -LiteralPath $ExtractRoot -Recurse -Filter "ffmpeg.exe" |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $ExtractedExe) {
        throw "Downloaded FFmpeg archive did not contain ffmpeg.exe."
    }

    $ExtractedRoot = Split-Path (Split-Path $ExtractedExe -Parent) -Parent
    if (Test-Path -LiteralPath $ToolRoot) {
        Remove-Item -LiteralPath $ToolRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $ToolRoot) | Out-Null
    Move-Item -LiteralPath $ExtractedRoot -Destination $ToolRoot

    if (-not (Test-Path -LiteralPath $FfmpegExe)) {
        throw "Could not prepare project FFmpeg at $FfmpegExe."
    }
    return $FfmpegExe
}

function Test-GptSoVitsPretrainedAssets {
    param([string]$RepoPath)

    $PretrainedRoot = Join-Path $RepoPath "GPT_SoVITS\pretrained_models"
    $TextRoot = Join-Path $RepoPath "GPT_SoVITS\text"
    return (
        (Test-Path -LiteralPath (Join-Path $PretrainedRoot "chinese-roberta-wwm-ext-large")) -and
        (Test-Path -LiteralPath (Join-Path $PretrainedRoot "chinese-hubert-base")) -and
        (Test-Path -LiteralPath (Join-Path $TextRoot "G2PWModel"))
    )
}

function Install-GptSoVitsPretrainedAssets {
    $RepoPath = Join-Path $ProjectRoot "third_party\GPT-SoVITS"
    if (-not (Test-Path -LiteralPath (Join-Path $RepoPath "api_v2.py"))) {
        throw "GPT-SoVITS repo is missing. Run bootstrap without -SkipThirdParty first."
    }

    if (Test-GptSoVitsPretrainedAssets -RepoPath $RepoPath) {
        Write-Host "GPT-SoVITS pretrained assets already exist."
        return
    }

    $DownloadRoot = Join-Path $ProjectRoot "storage\temporary\gpt_sovits"
    $PretrainedZip = Join-Path $DownloadRoot "pretrained_models.zip"
    $G2PwZip = Join-Path $DownloadRoot "G2PWModel.zip"

    $PretrainedRoot = Join-Path $RepoPath "GPT_SoVITS\pretrained_models"
    if (-not (
        (Test-Path -LiteralPath (Join-Path $PretrainedRoot "chinese-roberta-wwm-ext-large")) -and
        (Test-Path -LiteralPath (Join-Path $PretrainedRoot "chinese-hubert-base"))
    )) {
        Write-Host "Preparing GPT-SoVITS pretrained models..."
        Invoke-DownloadFile -Url $GptSoVitsPretrainedUrl -Destination $PretrainedZip -MinBytes 10000000 -ValidateZip
        Expand-ZipToDirectory -ZipPath $PretrainedZip -Destination (Join-Path $RepoPath "GPT_SoVITS")
    }

    $G2PwRoot = Join-Path $RepoPath "GPT_SoVITS\text\G2PWModel"
    if (-not (Test-Path -LiteralPath $G2PwRoot)) {
        Write-Host "Preparing GPT-SoVITS G2PWModel..."
        Invoke-DownloadFile -Url $GptSoVitsG2PwUrl -Destination $G2PwZip -MinBytes 1000000 -ValidateZip
        Expand-ZipToDirectory -ZipPath $G2PwZip -Destination (Join-Path $RepoPath "GPT_SoVITS\text")
    }

    if (-not (Test-GptSoVitsPretrainedAssets -RepoPath $RepoPath)) {
        throw "Downloaded GPT-SoVITS pretrained assets, but required directories are still missing."
    }
}

function Get-LlamaAsset {
    param([object]$Release, [string[]]$Patterns)
    foreach ($Pattern in $Patterns) {
        $Asset = $Release.assets |
            Where-Object { $_.name -match $Pattern } |
            Select-Object -First 1
        if ($Asset) {
            return $Asset
        }
    }
    return $null
}

function New-LlamaFallbackAsset {
    param([string]$Name)
    return [pscustomobject]@{
        name = $Name
        browser_download_url = "$LlamaFallbackAssetBaseUrl/$Name"
    }
}

function New-LlamaFallbackRelease {
    return [pscustomobject]@{
        tag_name = $LlamaFallbackReleaseTag
        assets = @(
            New-LlamaFallbackAsset -Name "llama-b10099-bin-win-cuda-12.4-x64.zip"
            New-LlamaFallbackAsset -Name "cudart-llama-bin-win-cuda-12.4-x64.zip"
        )
    }
}

function Resolve-LlamaRelease {
    Write-Host "Resolving latest llama.cpp Windows CUDA release assets..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        return Invoke-RestMethod `
            -Uri $LlamaReleaseApiUrl `
            -Headers @{
                "Accept" = "application/vnd.github+json"
                "User-Agent" = "astrbot-chat-bootstrap"
                "X-GitHub-Api-Version" = "2022-11-28"
            }
    } catch {
        Write-Host "Could not query llama.cpp latest release from GitHub API: $($_.Exception.Message)"
        Write-Host "Falling back to llama.cpp $LlamaFallbackReleaseTag CUDA 12.4 release assets."
        return New-LlamaFallbackRelease
    }
}

function Test-LlamaCudaRuntime {
    param([string]$ToolRoot)
    foreach ($Dll in $LlamaCudaRuntimeDlls) {
        if (-not (Test-Path -LiteralPath (Join-Path $ToolRoot $Dll))) {
            return $false
        }
    }
    return $true
}

function Install-ProjectLlamaCpp {
    $ToolRoot = Join-Path $ProjectRoot "tools\llama.cpp"
    $LlamaServer = Get-ChildItem -LiteralPath $ToolRoot -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if ($LlamaServer) {
        if (Test-LlamaCudaRuntime -ToolRoot $ToolRoot) {
            return $LlamaServer
        }
        Write-Host "Project llama-server exists, but CUDA runtime DLLs are missing. Reinstalling llama.cpp CUDA bundle..."
    }

    $Release = Resolve-LlamaRelease

    $BinAsset = Get-LlamaAsset -Release $Release -Patterns @(
        '^llama-.*-bin-win-cuda-12\..*-x64\.zip$',
        '^llama-.*-bin-win-cuda.*-x64\.zip$',
        '^llama-.*-bin-win-cublas.*-x64\.zip$',
        '^llama-.*-bin-win-vulkan-x64\.zip$',
        '^llama-.*-bin-win-avx2-x64\.zip$'
    )
    if (-not $BinAsset) {
        $Release = New-LlamaFallbackRelease
        $BinAsset = Get-LlamaAsset -Release $Release -Patterns @(
            '^llama-.*-bin-win-cuda-12\..*-x64\.zip$',
            '^llama-.*-bin-win-cuda.*-x64\.zip$'
        )
    }
    if (-not $BinAsset) {
        throw "Could not find a Windows x64 llama.cpp release archive containing llama-server.exe."
    }

    $RuntimeAsset = Get-LlamaAsset -Release $Release -Patterns @(
        '^cudart-llama-bin-win-cuda-12\..*-x64\.zip$',
        '^cudart-llama-bin-win-cuda.*-x64\.zip$',
        '^cudart-llama-.*-bin-win-cuda-12\..*-x64\.zip$',
        '^cudart-llama-.*-bin-win-cuda.*-x64\.zip$'
    )
    if (-not $RuntimeAsset) {
        $RuntimeAsset = Get-LlamaAsset -Release (New-LlamaFallbackRelease) -Patterns @(
            '^cudart-llama-bin-win-cuda-12\..*-x64\.zip$',
            '^cudart-llama-bin-win-cuda.*-x64\.zip$'
        )
    }

    $DownloadRoot = Join-Path $ProjectRoot "storage\temporary\llama.cpp"
    $ExtractRoot = Join-Path $ProjectRoot "storage\temporary\llama.cpp-extract"
    if (Test-Path -LiteralPath $ExtractRoot) {
        Remove-Item -LiteralPath $ExtractRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $ExtractRoot | Out-Null

    $BinZip = Join-Path $DownloadRoot $BinAsset.name
    Invoke-DownloadFile -Url $BinAsset.browser_download_url -Destination $BinZip -MinBytes 1000000 -ValidateZip
    Expand-ZipToDirectory -ZipPath $BinZip -Destination $ExtractRoot

    if ($RuntimeAsset) {
        $RuntimeZip = Join-Path $DownloadRoot $RuntimeAsset.name
        Invoke-DownloadFile -Url $RuntimeAsset.browser_download_url -Destination $RuntimeZip -MinBytes 1000000 -ValidateZip
        Expand-ZipToDirectory -ZipPath $RuntimeZip -Destination $ExtractRoot
    }

    if (Test-Path -LiteralPath $ToolRoot) {
        Remove-Item -LiteralPath $ToolRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $ToolRoot | Out-Null
    Get-ChildItem -LiteralPath $ExtractRoot -Force | ForEach-Object {
        Move-Item -LiteralPath $_.FullName -Destination $ToolRoot
    }

    $LlamaServer = Get-ChildItem -LiteralPath $ToolRoot -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $LlamaServer) {
        throw "Downloaded llama.cpp archive did not contain llama-server.exe."
    }
    if (-not (Test-LlamaCudaRuntime -ToolRoot $ToolRoot)) {
        throw "Downloaded llama.cpp archive did not contain required CUDA runtime DLLs: $($LlamaCudaRuntimeDlls -join ', ')."
    }
    return $LlamaServer
}

function Write-OptionalToolStatus {
    param([string]$Name, [scriptblock]$Probe, [string]$Hint)
    try {
        $Ok = & $Probe
    } catch {
        $Ok = $false
    }

    if ($Ok) {
        Write-Host "[OK] $Name"
    } else {
        Write-Host "[TODO] $Name - $Hint"
    }
}

Push-Location $ProjectRoot
try {
    # Model weights are local assets under model_store and are not pulled from Git LFS.

    if ($SkipThirdParty) {
        Write-Host "Skipping third-party source download."
    } else {
        Install-GptSoVitsRepo
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\patch_gpt_sovits_windows.ps1")

    $FfmpegExe = Install-ProjectFfmpeg
    Write-Host "Using project FFmpeg: $FfmpegExe"
    $LlamaServerExe = Install-ProjectLlamaCpp
    Write-Host "Using project llama-server: $LlamaServerExe"

    $RuntimePython = Resolve-OrCreateRuntimePython -PythonOverride $Python
    Write-Host "Using unified runtime Python: $RuntimePython"
    Invoke-Pip -Python $RuntimePython -Arguments @("install", "--upgrade", "pip", "wheel", "setuptools<82")
    if (-not $Lightweight) {
        Install-CudaTorch -Python $RuntimePython
    }

    $RequirementFiles = @(
        "runtime/audio/requirements.txt",
        "runtime/qwen3/requirements.txt",
        "runtime/gpt_sovits/requirements.txt",
        "runtime/app/requirements.txt"
    )
    if ($Lightweight) {
        $RequirementFiles += "runtime/sensevoice/requirements.txt"
    } else {
        $RequirementFiles += "runtime/sensevoice/requirements-model.txt"
    }
    Install-Requirements -Python $RuntimePython -RequirementFiles $RequirementFiles

    $GptSoVitsRequirements = Join-Path $ProjectRoot "third_party\GPT-SoVITS\requirements.txt"
    if (Test-Path -LiteralPath $GptSoVitsRequirements) {
        $PlatformGptSoVitsRequirements = New-GptSoVitsPlatformRequirements `
            -Source $GptSoVitsRequirements `
            -Destination (Join-Path $ProjectRoot "storage\temporary\gpt_sovits_requirements.resolved.txt")
        Write-Host "Installing GPT-SoVITS third-party requirements from $PlatformGptSoVitsRequirements"
        Invoke-Pip -Python $RuntimePython -Arguments @("install", "-r", $PlatformGptSoVitsRequirements)
    }

    Invoke-Pip -Python $RuntimePython -Arguments @("install", "pytest", "torchcodec", "opencc-python-reimplemented")

    if ($SkipModelDownloads) {
        Write-Host "Skipping Qwen3, SenseVoice, and GPT-SoVITS pretrained model downloads."
    } else {
        Install-GptSoVitsPretrainedAssets

        Write-Host "Preparing Qwen3 model files..."
        Invoke-RuntimePython -Python $RuntimePython -Arguments @("scripts\download_qwen3_model.py")

        if ($Lightweight) {
            Write-Host "Skipping SenseVoice model download because -Lightweight was provided."
        } else {
            Write-Host "Preparing SenseVoice model files..."
            Invoke-RuntimePython -Python $RuntimePython -Arguments @("scripts\download_sensevoice_model.py")
        }
    }

    Write-Host ""
    Write-Host "Project-local runtime checks:"
    Write-OptionalToolStatus `
        -Name "FFmpeg under tools/ffmpeg" `
        -Probe { Test-Path (Join-Path $ProjectRoot "tools\ffmpeg\bin\ffmpeg.exe") } `
        -Hint "Run quickstart/bootstrap.ps1 to download FFmpeg."
    Write-OptionalToolStatus `
        -Name "llama-server under tools/llama.cpp" `
        -Probe {
            [bool](Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "tools\llama.cpp") -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue)
        } `
        -Hint "Run quickstart/bootstrap.ps1 to download llama.cpp."
    Write-OptionalToolStatus `
        -Name "GPT-SoVITS upstream repo" `
        -Probe { Test-Path (Join-Path $ProjectRoot "third_party\GPT-SoVITS\api_v2.py") } `
        -Hint "Prepare third_party/GPT-SoVITS before starting TTS."
    Write-OptionalToolStatus `
        -Name "Qwen3 model files" `
        -Probe { Test-Path (Join-Path $ProjectRoot "model_store\registry\qwen3\elysia\qwen3-14b-q4-k-m-base-v1\Qwen3-14B-Q4_K_M.gguf") } `
        -Hint "Run quickstart/bootstrap.ps1 to download Qwen3."
    Write-OptionalToolStatus `
        -Name "GPT-SoVITS pretrained assets" `
        -Probe { Test-GptSoVitsPretrainedAssets -RepoPath (Join-Path $ProjectRoot "third_party\GPT-SoVITS") } `
        -Hint "Run quickstart/bootstrap.ps1 to download GPT-SoVITS upstream pretrained assets."
    if ($Lightweight) {
        Write-Host "[SKIP] SenseVoice model files - lightweight bootstrap"
    } else {
        Write-OptionalToolStatus `
            -Name "SenseVoice model files" `
            -Probe { Test-Path (Join-Path $ProjectRoot "model_store\registry\sensevoice\sensevoice-small\base-v1\model.pt") } `
            -Hint "Run quickstart/bootstrap.ps1 without -Lightweight to download SenseVoice."
    }

    if (-not $SkipDoctor) {
        Write-Host ""
        & $RuntimePython scripts/doctor.py
    }

    Write-Host ""
    Write-Host "Bootstrap complete. Start with:"
    Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/run.ps1"
    Write-Host ""
    Write-Host "For Qwen3 training, run after bootstrap:"
    Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/install_qwen3_training_deps.ps1"
    Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/download_qwen3_training_model.ps1"
} finally {
    Pop-Location
}
