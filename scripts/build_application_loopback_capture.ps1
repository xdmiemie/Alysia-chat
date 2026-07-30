param(
    [string]$Output = "tools/application_loopback_capture/application_loopback_capture.exe"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Source = Join-Path $ProjectRoot "tools\application_loopback_capture\application_loopback_capture.cpp"
$OutputPath = Join-Path $ProjectRoot $Output
$OutputDir = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
Remove-Item -LiteralPath $OutputPath -ErrorAction SilentlyContinue

function Find-VsDevCmd {
    $Candidates = @(
        "$env:ProgramFiles\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat",
        "$env:ProgramFiles\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat",
        "$env:ProgramFiles\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat",
        "$env:ProgramFiles\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat"
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            return $Candidate
        }
    }
    $VsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $VsWhere) {
        $InstallPath = & $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if (-not [string]::IsNullOrWhiteSpace($InstallPath)) {
            $Candidate = Join-Path $InstallPath "Common7\Tools\VsDevCmd.bat"
            if (Test-Path -LiteralPath $Candidate) {
                return $Candidate
            }
        }
    }
    return ""
}

$VsDevCmd = Find-VsDevCmd
if ([string]::IsNullOrWhiteSpace($VsDevCmd)) {
    throw "MSVC Build Tools were not found. Install Visual Studio 2022 Build Tools with the Desktop C++ workload, then rerun this script."
}

$BuildCmd = Join-Path $env:TEMP "alysia_build_application_loopback_capture.cmd"
@"
@echo off
call "$VsDevCmd" -arch=x64 -host_arch=x64
where cl
cl /nologo /std:c++17 /EHsc /O2 /DUNICODE /D_UNICODE "$Source" /Fe:"$OutputPath" /link ole32.lib avrt.lib Mmdevapi.lib
"@ | Set-Content -Path $BuildCmd -Encoding ASCII

cmd.exe /c "`"$BuildCmd`""
if ($LASTEXITCODE -ne 0) {
    throw "MSVC build failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $OutputPath)) {
    throw "Build did not produce $OutputPath"
}
Write-Host "Built $OutputPath"
