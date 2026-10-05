param([switch]$Open, [string]$DataDirectory = '', [int]$Port = 18741)
. (Join-Path $PSScriptRoot 'windows_common.ps1')
$meterRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$meterAddress = 'http://127.0.0.1:' + $Port
try {
    if ($Port -lt 1024 -or $Port -gt 65535) { throw '端口必须在 1024 到 65535 之间。' }
    $meterHealth = $null
    try { $meterHealth = Invoke-RestMethod -Uri ($meterAddress + '/api/health') -TimeoutSec 1 } catch {}
    if ($meterHealth) {
        if ($meterHealth.application -ne 'token-meter' -or $meterHealth.storage_directory -ne $meterRoot) {
            throw ('端口 ' + $Port + ' 已由另一个程序或另一份看板使用。请先停止那份看板。')
        }
    } else {
        $meterTask = if ($Port -eq 18741) { Get-MeterTask $meterRoot } else { $null }
        if ($meterTask) {
            Enable-ScheduledTask -TaskName $meterTask.TaskName | Out-Null
            Start-ScheduledTask -TaskName $meterTask.TaskName
        } else {
            $meterPython = Get-MeterPython $meterRoot $null
            $meterPythonw = Join-Path (Split-Path $meterPython -Parent) 'pythonw.exe'
            if (-not (Test-Path -LiteralPath $meterPythonw)) { throw '运行环境缺少 pythonw.exe。' }
            if (-not $DataDirectory) { $DataDirectory = Join-Path $meterRoot 'data' }
            $meterDatabase = Join-Path ([IO.Path]::GetFullPath($DataDirectory)) 'token-meter.sqlite3'
            if ($meterDatabase.Contains('"') -or $meterRoot.Contains('"')) { throw '路径不能包含双引号。' }
            $meterArguments = '-B "' + (Join-Path $meterRoot 'server.py') + '" --database "' + $meterDatabase + '" --port ' + $Port
            Start-Process -FilePath $meterPythonw -ArgumentList $meterArguments -WorkingDirectory $meterRoot -WindowStyle Hidden
        }
        $meterReady = $false
        for ($meterAttempt = 0; $meterAttempt -lt 60; $meterAttempt++) {
            try {
                $meterHealth = Invoke-RestMethod -Uri ($meterAddress + '/api/health') -TimeoutSec 1
                if ($meterHealth.application -eq 'token-meter' -and $meterHealth.storage_directory -eq $meterRoot) { $meterReady = $true; break }
            } catch {}
            Start-Sleep -Milliseconds 250
        }
        if (-not $meterReady) { throw '看板未能启动。请检查 data\service-error.log；确认文件夹可写并且端口未被占用。' }
    }
    # A stopped task stays disabled until this directory's own start entry enables it.
    $meterTask = if ($Port -eq 18741) { Get-MeterTask $meterRoot } else { $null }
    if ($meterTask) { Enable-ScheduledTask -TaskName $meterTask.TaskName | Out-Null }
    if ($Open) { Start-Process $meterAddress }
} catch {
    Show-MeterError $_.Exception.Message
    exit 1
}
