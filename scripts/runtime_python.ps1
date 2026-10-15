function Resolve-AlysiaRuntimePython {
    param([string]$PythonOverride = "")

    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

    if (-not [string]::IsNullOrWhiteSpace($PythonOverride)) {
        if (-not (Test-Path -LiteralPath $PythonOverride)) {
            throw "Runtime Python was provided but does not exist: $PythonOverride"
        }
        return (Resolve-Path -LiteralPath $PythonOverride).Path
    }

    $ProjectRuntimeCandidates = @(
        (Join-Path $ProjectRoot "runtime_env\python.exe"),
        (Join-Path $ProjectRoot "runtime_env\Scripts\python.exe")
    )
    foreach ($ProjectRuntime in $ProjectRuntimeCandidates) {
        if (Test-Path -LiteralPath $ProjectRuntime) {
            return (Resolve-Path -LiteralPath $ProjectRuntime).Path
        }
    }

    foreach ($EnvVarName in @("ALYSIA_PYTHON", "GPT_SOVITS_PYTHON")) {
        $Value = [Environment]::GetEnvironmentVariable($EnvVarName)
        if (-not [string]::IsNullOrWhiteSpace($Value)) {
            if (-not (Test-Path -LiteralPath $Value)) {
                throw "${EnvVarName} points to a missing file: $Value"
            }
            return (Resolve-Path -LiteralPath $Value).Path
        }
    }

    throw "Could not find runtime Python. Prepare runtime_env\python.exe, prepare runtime_env\Scripts\python.exe, or set ALYSIA_PYTHON/GPT_SOVITS_PYTHON."
}

function Resolve-AlysiaQwenPython {
    param([string]$PythonOverride = "")

    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    if (-not [string]::IsNullOrWhiteSpace($PythonOverride)) {
        if (-not (Test-Path -LiteralPath $PythonOverride -PathType Leaf)) {
            throw "Qwen3 Python was provided but does not exist: $PythonOverride"
        }
        return (Resolve-Path -LiteralPath $PythonOverride).Path
    }

    $Configured = [Environment]::GetEnvironmentVariable("ALYSIA_QWEN_PYTHON")
    if (-not [string]::IsNullOrWhiteSpace($Configured)) {
        if (-not (Test-Path -LiteralPath $Configured -PathType Leaf)) {
            throw "ALYSIA_QWEN_PYTHON points to a missing file: $Configured"
        }
        return (Resolve-Path -LiteralPath $Configured).Path
    }

    foreach ($Candidate in @(
        (Join-Path $ProjectRoot "runtime_env_qwen\python.exe"),
        (Join-Path $ProjectRoot "runtime_env_qwen\Scripts\python.exe")
    )) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }

    throw "Qwen3 Python is missing. Run quickstart/bootstrap.ps1 to prepare runtime_env_qwen."
}
