# 重启 5330 应用服务并核验（需管理员 PowerShell）。
#
# 为什么需要它：Python 在进程启动时就把模块读进内存了，改完 src/ 下的代码
# **不重启服务是不会生效的**（venv 是 editable 安装，重启即加载新代码）。
#
# 用法（以管理员身份运行）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File "D:\瀚思AI客服\scripts\restart_app.ps1"
#
# 它做三件事：重启前只读检查会话状态库（避免把未终态任务带进首次轮询）→ 重启服务 → 轮询 /health。

$ErrorActionPreference = "Continue"
# 中英混排输出：固定为 UTF-8，避免控制台代码页把中文显示成乱码。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$root = Split-Path -Parent $PSScriptRoot
$service = "FastGptAssistant"
$healthUrl = "http://127.0.0.1:5330/health"

Write-Host "=== 1. 权限检查 ==="
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "  [失败] 当前会话未提权，无法控制 Windows 服务。"
    Write-Host "  请用「以管理员身份运行」打开 PowerShell 后重试。"
    exit 1
}
Write-Host "  已提权 ($($identity.Name))"

Write-Host ""
Write-Host "=== 2. 重启前检查未终态任务（只读）==="
$stateScript = Join-Path $PSScriptRoot "check_pending_tasks.py"
$python = Join-Path $root ".venv\Scripts\python.exe"
if ((Test-Path $stateScript) -and (Test-Path $python)) {
    Push-Location $root
    & $python $stateScript
    Pop-Location
    Write-Host "  （若上面列出未终态任务，重启后首次轮询可能立即处理它们）"
} else {
    Write-Host "  跳过：未找到 $stateScript"
}

Write-Host ""
Write-Host "=== 3. 重启服务 ==="
try {
    Restart-Service -Name $service -Force -ErrorAction Stop
    Write-Host "  ${service}: 重启指令已下发"
} catch {
    Write-Host "  [失败] $($_.Exception.Message)"
    exit 1
}

Write-Host ""
Write-Host "=== 4. 等待并核验 ==="
$healthy = $false
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name $service -ErrorAction SilentlyContinue
    try {
        $resp = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 10
        Write-Host "  [第 $i 次] ${service}=$($svc.Status)  /health status=$($resp.status)"
        if ($resp.status -eq "healthy") { $healthy = $true; break }
    } catch {
        Write-Host "  [第 $i 次] ${service}=$($svc.Status)  /health 尚未就绪"
    }
}

Write-Host ""
if ($healthy) {
    Write-Host "=== 结果：服务已重启并通过健康检查 ==="
} else {
    Write-Host "=== 结果：健康检查未通过，请查看 logs\5330.err.log ==="
    exit 1
}


