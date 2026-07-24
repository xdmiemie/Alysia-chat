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

    throw "Could not find unified runtime Python. Prepare runtime_env\python.exe, prepare runtime_env\Scripts\python.exe, or set ALYSIA_PYTHON/GPT_SOVITS_PYTHON."
}
