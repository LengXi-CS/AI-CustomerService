# 每日 7 点由计划任务调用：从飞书全量同步主数据读模型。
# 输出追加写入 logs\sync-bitable.log，便于事后排查。

$root = "D:\瀚思AI客服"
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$log = Join-Path $logDir "sync-bitable.log"

Set-Location $root
"===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 开始飞书数据同步 =====" |
    Out-File -FilePath $log -Append -Encoding utf8

& "$root\.venv\Scripts\python.exe" "scripts\sync_bitable.py" *>> $log

"----- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 同步结束 退出码=$LASTEXITCODE -----" |
    Out-File -FilePath $log -Append -Encoding utf8
