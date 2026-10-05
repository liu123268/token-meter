$ErrorActionPreference = 'Stop'
function Get-MeterTask([string]$Root) {
    $task = Get-ScheduledTask -TaskName 'TokenMeter-LocalDashboard' -ErrorAction SilentlyContinue
    $script = Join-Path $Root 'server.py'
    if ($task -and @($task.Actions | Where-Object {
        $_.Arguments -and $_.Arguments.IndexOf(('"' + $script + '"'), [StringComparison]::OrdinalIgnoreCase) -ge 0
    }).Count -gt 0) { return $task }
    return $null
}
function Get-MeterPython([string]$Root, $Task) {
    $candidates = @((Join-Path $Root 'runtime\python.exe'))
    if ($Task) {
        foreach ($action in $Task.Actions) { $candidates += Join-Path (Split-Path $action.Execute -Parent) 'python.exe' }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*\Microsoft\WindowsApps\*') { $candidates += $command.Source }
    foreach ($registry in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem -LiteralPath $registry -ErrorAction SilentlyContinue)) {
            $key = Get-Item -LiteralPath ($version.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
            if ($key) { $candidates += Join-Path $key.GetValue('') 'python.exe' }
        }
    }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            & $candidate -I -B -c 'import sys; from compression import zstd; raise SystemExit(0 if sys.version_info >= (3,14) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch {}
    }
    throw '没有找到 Python 3.14 运行环境。请使用包含 runtime 的 Windows 便携包，或安装 Python 3.14。'
}
function Show-MeterError([string]$Message) {
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($Message, 'Token 看板') | Out-Null
    } catch { Write-Host $Message }
}
