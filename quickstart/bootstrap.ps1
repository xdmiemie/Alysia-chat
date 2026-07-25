param(
    [string]$Python = "",
    [switch]$Lightweight,
    [switch]$SkipLfsPull,
    [switch]$SkipThirdParty,
    [switch]$SkipModelDownloads,
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $ProjectRoot "scripts\runtime_python.ps1")

$BundledPythonVersion = "3.10.11"
$BundledPythonUrl = "https://www.python.org/ftp/python/$BundledPythonVersion/python-$BundledPythonVersion-amd64.exe"
$FfmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$LlamaReleaseApiUrl = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
$GptSoVitsRepoUrl = "https://github.com/RVC-Boss/GPT-SoVITS.git"
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
        Write-Host "Downloading Python $BundledPythonVersion from python.org..."
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $BundledPythonUrl -OutFile $Installer
        } catch {
            throw "Could not download Python $BundledPythonVersion from $BundledPythonUrl. Install Python 3.10 manually, add it to PATH or the Windows py launcher, then rerun quickstart/bootstrap.ps1. Error: $($_.Exception.Message)"
        }
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
        return Resolve-AlysiaRuntimePython -PythonOverride $PythonOverride
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

function Invoke-GitLfsPull {
    $Git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $Git) {
        throw "Could not find git. Install Git for Windows, then rerun quickstart/bootstrap.ps1."
    }

    & $Git.Source lfs version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Git LFS is not available. Install Git LFS, then rerun quickstart/bootstrap.ps1."
    }

    Write-Host "Pulling Git LFS model files..."
    & $Git.Source lfs install --local | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "git lfs install --local failed."
    }
    & $Git.Source lfs pull
    if ($LASTEXITCODE -ne 0) {
        throw "git lfs pull failed."
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
    param([string]$Url, [string]$Destination, [int64]$MinBytes = 1)

    if (Test-Path -LiteralPath $Destination) {
        $Existing = Get-Item -LiteralPath $Destination
        if ($Existing.Length -ge $MinBytes) {
            return
        }
        Remove-Item -LiteralPath $Destination -Force
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $Destination) | Out-Null
    Write-Host "Downloading $Url"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $Destination
    } catch {
        $Curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        if (-not $Curl) {
            throw "Could not download $Url. Error: $($_.Exception.Message)"
        }
        Write-Host "Invoke-WebRequest failed; retrying with curl.exe..."
        & $Curl.Source --location --fail --retry 3 --output $Destination $Url
        if ($LASTEXITCODE -ne 0) {
            throw "Could not download $Url with curl.exe. Exit code: $LASTEXITCODE"
        }
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
    Invoke-DownloadFile -Url $FfmpegUrl -Destination $ZipPath -MinBytes 80000000
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

    Write-Host "Resolving latest llama.cpp Windows CUDA release assets..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $Release = Invoke-RestMethod -Uri $LlamaReleaseApiUrl
    } catch {
        throw "Could not query llama.cpp latest release from $LlamaReleaseApiUrl. Error: $($_.Exception.Message)"
    }

    $BinAsset = Get-LlamaAsset -Release $Release -Patterns @(
        '^llama-.*-bin-win-cuda-12\..*-x64\.zip$',
        '^llama-.*-bin-win-cuda.*-x64\.zip$',
        '^llama-.*-bin-win-cublas.*-x64\.zip$',
        '^llama-.*-bin-win-vulkan-x64\.zip$',
        '^llama-.*-bin-win-avx2-x64\.zip$'
    )
    if (-not $BinAsset) {
        throw "Could not find a Windows x64 llama.cpp release archive containing llama-server.exe."
    }

    $RuntimeAsset = Get-LlamaAsset -Release $Release -Patterns @(
        '^cudart-llama-bin-win-cuda-12\..*-x64\.zip$',
        '^cudart-llama-bin-win-cuda.*-x64\.zip$',
        '^cudart-llama-.*-bin-win-cuda-12\..*-x64\.zip$',
        '^cudart-llama-.*-bin-win-cuda.*-x64\.zip$'
    )

    $DownloadRoot = Join-Path $ProjectRoot "storage\temporary\llama.cpp"
    $ExtractRoot = Join-Path $ProjectRoot "storage\temporary\llama.cpp-extract"
    if (Test-Path -LiteralPath $ExtractRoot) {
        Remove-Item -LiteralPath $ExtractRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $ExtractRoot | Out-Null

    $BinZip = Join-Path $DownloadRoot $BinAsset.name
    Invoke-DownloadFile -Url $BinAsset.browser_download_url -Destination $BinZip -MinBytes 1000000
    Expand-ZipToDirectory -ZipPath $BinZip -Destination $ExtractRoot

    if ($RuntimeAsset) {
        $RuntimeZip = Join-Path $DownloadRoot $RuntimeAsset.name
        Invoke-DownloadFile -Url $RuntimeAsset.browser_download_url -Destination $RuntimeZip -MinBytes 1000000
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
    if ($SkipLfsPull) {
        Write-Host "Skipping Git LFS pull."
    } else {
        Invoke-GitLfsPull
    }

    if ($SkipThirdParty) {
        Write-Host "Skipping third-party source download."
    } else {
        Install-GptSoVitsRepo
    }

    $FfmpegExe = Install-ProjectFfmpeg
    Write-Host "Using project FFmpeg: $FfmpegExe"
    $LlamaServerExe = Install-ProjectLlamaCpp
    Write-Host "Using project llama-server: $LlamaServerExe"

    $RuntimePython = Resolve-OrCreateRuntimePython -PythonOverride $Python
    Write-Host "Using unified runtime Python: $RuntimePython"
    Invoke-Pip -Python $RuntimePython -Arguments @("install", "--upgrade", "pip", "wheel", "setuptools<82")

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
    Invoke-Pip -Python $RuntimePython -Arguments @("install", "pytest", "torchcodec", "opencc-python-reimplemented")

    if ($SkipModelDownloads) {
        Write-Host "Skipping Qwen3 and SenseVoice model downloads."
    } else {
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
        -Probe { Test-Path (Join-Path $ProjectRoot "model_store\registry\qwen3\qwen3-8b\q4-k-m-base-v1\Qwen3-8B-Q4_K_M.gguf") } `
        -Hint "Run quickstart/bootstrap.ps1 to download Qwen3."
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
    Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/run.ps1 -Character elysia"
} finally {
    Pop-Location
}
