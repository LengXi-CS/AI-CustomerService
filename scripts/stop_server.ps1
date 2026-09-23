# PowerShell 停止脚本：停止端口 5330 的 FastGpt Assistant 进程

$existing = Get-NetTCPConnection -LocalPort 5330 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $existing | ForEach-Object {
        $pidToStop = $_.OwningProcess
        Write-Host "正在终止端口 5330 对应服务进程 (PID: $pidToStop)..."
        Stop-Process -Id $pidToStop -Force
        Write-Host "服务进程 (PID: $pidToStop) 已成功停止。"
    }
} else {
    Write-Host "当前端口 5330 未检测到正在运行的服务。"
}
