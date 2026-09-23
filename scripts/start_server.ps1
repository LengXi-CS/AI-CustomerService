# PowerShell 启动脚本：在后台静默运行 FastGpt Assistant (端口 5330)

$ErrorActionPreference = "Stop"

# 确保在项目根目录
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# 确保 logs 目录存在
if (-not (Test-Path "logs")) {
    New-Item -ItemType Directory -Path "logs" | Out-Null
}

# 杀掉可能占用的 5330 旧进程
$existing = Get-NetTCPConnection -LocalPort 5330 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $existing | ForEach-Object {
        Write-Host "正在终止占用端口 5330 的旧进程 (PID: $($_.OwningProcess))..."
        Stop-Process -Id $_.OwningProcess -Force
    }
    Start-Sleep -Seconds 1
}

# 优先读取虚拟环境中的 python
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    Write-Error "未找到虚拟环境 Python: $PythonExe，请先运行 'uv sync'"
    exit 1
}

# 启动后台进程
$proc = Start-Process -FilePath $PythonExe `
    -ArgumentList "-m", "uvicorn", "fastgpt_assistant.server.app:app", "--host", "0.0.0.0", "--port", "5330" `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $ProjectRoot "logs\5330.out.log") `
    -RedirectStandardError (Join-Path $ProjectRoot "logs\5330.err.log")

Write-Host "=========================================="
Write-Host " FastGPT Assistant 服务已成功在后台启动！"
Write-Host " 进程 PID: $($proc.Id)"
Write-Host " 监听端口: 5330"
Write-Host " 标准输出: logs\5330.out.log"
Write-Host " 错误日志: logs\5330.err.log"
Write-Host "=========================================="
