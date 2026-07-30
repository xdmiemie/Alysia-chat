$ErrorActionPreference = "Stop"
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like "python*" -and $_.CommandLine -like "*runtime.adapters.wechat.app*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
    }
