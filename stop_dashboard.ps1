. (Join-Path $PSScriptRoot 'windows_common.ps1')
$meterRoot = [IO.Path]::GetFullPath($PSScriptRoot)
try {
    $meterTask = Get-MeterTask $meterRoot
    if ($meterTask) {
        Disable-ScheduledTask -TaskName $meterTask.TaskName | Out-Null
        Stop-ScheduledTask -TaskName $meterTask.TaskName
    }
    $meterScript = '"' + (Join-Path $meterRoot 'server.py') + '"'
    $meterProcesses = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
    foreach ($meterProcess in $meterProcesses) {
        if ($meterProcess.CommandLine -and $meterProcess.CommandLine.IndexOf($meterScript, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            Stop-Process -Id $meterProcess.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host '本目录的看板已停止，数据保留。如有属于本目录的自动启动任务，也已暂停。'
} catch { Show-MeterError $_.Exception.Message; exit 1 }
