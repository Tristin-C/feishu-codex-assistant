$ErrorActionPreference = "Stop"
$TaskName = "FeishuCodexBridge"
$AppDir = Join-Path $env:LOCALAPPDATA "FeishuCodexBridge"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*$AppDir*bridge.py*serve*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$answer = Read-Host "是否同时删除任务记录和配置？输入 YES 删除，直接回车保留"
if ($answer -eq "YES") {
    Remove-Item -LiteralPath $AppDir -Recurse -Force
} else {
    Write-Host "已停止后台服务和开机启动，保留 $AppDir"
}

