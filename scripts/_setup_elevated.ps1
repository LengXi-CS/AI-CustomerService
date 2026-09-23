# 需要管理员权限：注册/修复两个 Windows 服务（5330 应用 + cpolar 隧道）+ 每日 7 点数据同步计划任务。
#
# 修复记录
#   2026-09-20 (v1→v2)：形参原名 $Args，与 PowerShell 自动变量冲突、在带 param() 的函数里恒为空，
#     导致 nssm 收不到 AppParameters —— 服务能装能起但什么都没跑。已改名 $AppArgs 并显式 set 落库。
#   2026-09-20 (v2→v3)：v2 采用 `nssm remove` + `nssm install` 重建，第二次运行撞上 SCM 的
#     "marked for deletion" 竞态，install 报 Error creating service，把 FastGptAssistant 删了却没建回来。
#     改为幂等、不删服务：已存在就只改参数；参数没变就不重启（cpolar 免费域名一重启就漂移）。
#   2026-09-20 (v3→v4)：
#     a) 公网地址改从 **cpolar 本机 API(127.0.0.1:4040)** 取（权威），日志解析仅作兜底 ——
#        v3 读日志时正赶上 nssm 轮转日志，把已失效的旧域名写进了 .env。
#     b) **服务在跑时不再清理 cpolar 进程**：cpolar 会 fork，v3 的"排除服务 PID"只排除了父进程，
#        误杀了真正持有隧道的子进程，导致隧道重连、免费域名又漂一次。
#     c) .env 同步后**显式重启应用服务**，确保它读到最新地址（应用只在启动时读 .env）。
#        重启应用不会影响 cpolar 域名。
#
# 结果写入 _setup_result.txt，供非提权会话读取。

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$root      = "D:\瀚思AI客服"
$nssm      = "D:\tools\nssm-2.24\win64\nssm.exe"
$venvPy    = Join-Path $root ".venv\Scripts\python.exe"
$cpolarCfg = "C:\Users\THINK\.cpolar\cpolar.yml"
$result    = Join-Path $root "_setup_result.txt"

$svcApp    = "FastGptAssistant"
$svcTunnel = "cpolar-website"

$lines = @()
$lines += "=== 时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
$lines += "提权: " + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$lines += "账号: " + ([Security.Principal.WindowsIdentity]::GetCurrent()).Name

# --- 工具函数 ---------------------------------------------------------------

function Get-SvcParam {
    param($Name, $Key)
    try {
        $pr = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$Name\Parameters" -ErrorAction Stop
        if ($pr.PSObject.Properties.Name -contains $Key) { return [string]$pr.$Key }
    } catch { }
    return $null
}

function Read-Shared {
    # 共享方式读日志：cpolar 一直持有 logs\cpolar.out.log，普通 ReadAllText 会抛 IOException
    param($Path)
    if (-not (Test-Path $Path)) { return "" }
    try {
        $fs = [System.IO.FileStream]::new($Path, [System.IO.FileMode]::Open,
                                          [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $sr = [System.IO.StreamReader]::new($fs, [System.Text.Encoding]::UTF8)
        $t = $sr.ReadToEnd()
        $sr.Close(); $fs.Close()
        return $t
    } catch { return "" }
}

function Get-CpolarPublicUrl {
    # 权威来源：cpolar 本机 API（127.0.0.1:4040）。
    # 用 venv 里的 Python 去取 —— PowerShell 的 Invoke-WebRequest 在这台机器上捕获响应内容
    # 不稳定（实测会拿到空/截断内容），而日志又会被 nssm 截断轮转、新旧交织。
    $py = Join-Path $root "scripts\get_public_url.py"
    if (Test-Path $py) {
        $o = ((& $venvPy $py 2>$null | Out-String)).Trim()
        if ($LASTEXITCODE -eq 0 -and $o -match '^https://') {
            return @{ Url = $o; Source = "cpolar API (via venv python)" }
        }
    }
    $clog = Read-Shared (Join-Path $root "logs\cpolar.out.log")
    $m2 = [regex]::Matches($clog, 'Tunnel established at (https://[0-9A-Za-z\.\-]+)')
    if ($m2.Count -gt 0) {
        return @{ Url = $m2[$m2.Count - 1].Groups[1].Value; Source = "日志兜底(共 $($m2.Count) 条)" }
    }
    return @{ Url = ""; Source = "未发现" }
}

function Ensure-Service {
    # 幂等：不存在才安装（带重试）；存在就只改参数。返回是否需要重启。
    param($Name, $App, $AppArgs, $OutLog, $ErrLog)

    if (-not (Get-Service -Name $Name -ErrorAction SilentlyContinue)) {
        $ok = $false
        for ($try = 1; $try -le 6; $try++) {
            $r = ((& $nssm install $Name $App 2>&1 | Out-String).Trim())
            Start-Sleep -Seconds 3
            if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
                $script:lines += "  $Name 安装成功（第 $try 次尝试）"
                $ok = $true; break
            }
            $script:lines += "  $Name 安装第 $try 次失败: $r"
        }
        if (-not $ok) { $script:lines += "  !! $Name 安装失败，放弃"; return $false }
    } else {
        $script:lines += "  $Name 已存在，跳过安装（不删服务，规避 SCM 竞态）"
    }

    $changed = ((Get-SvcParam $Name "Application")   -ne $App) -or
               ((Get-SvcParam $Name "AppParameters") -ne $AppArgs) -or
               ((Get-SvcParam $Name "AppDirectory")  -ne $root)

    & $nssm set $Name Application $App 2>&1 | Out-Null
    & $nssm set $Name AppParameters $AppArgs 2>&1 | Out-Null
    & $nssm set $Name AppDirectory $root 2>&1 | Out-Null
    & $nssm set $Name AppStdout $OutLog 2>&1 | Out-Null
    & $nssm set $Name AppStderr $ErrLog 2>&1 | Out-Null
    & $nssm set $Name AppRotateFiles 1 2>&1 | Out-Null
    & $nssm set $Name AppRotateBytes 10485760 2>&1 | Out-Null
    & $nssm set $Name AppExit Default Restart 2>&1 | Out-Null
    & $nssm set $Name AppRestartDelay 5000 2>&1 | Out-Null
    & $nssm set $Name AppThrottle 5000 2>&1 | Out-Null
    & $nssm set $Name AppStopMethodConsole 15000 2>&1 | Out-Null
    & $nssm set $Name Start SERVICE_AUTO_START 2>&1 | Out-Null

    return $changed
}

function Start-Needed {
    # 参数变了 → 重启；没变且已在跑 → 一动不动（保住 cpolar 免费域名）
    param($Name, $NeedRestart)
    $s = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $s) { $script:lines += "  ${Name}: 服务不存在，无法启动"; return }
    if ($s.Status -eq "Running") {
        if ($NeedRestart) {
            $script:lines += "  ${Name}: 参数有变，重启中…"
            Restart-Service -Name $Name -Force -ErrorAction SilentlyContinue
        } else {
            $script:lines += "  ${Name}: 已在运行且参数一致，保持不动"
            return
        }
    } else {
        $script:lines += "  ${Name}: 启动中（当前 $($s.Status)）…"
        Start-Service -Name $Name -ErrorAction SilentlyContinue
    }
    for ($i = 1; $i -le 20; $i++) {
        Start-Sleep -Seconds 3
        $s = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($s -and $s.Status -eq "Running") { $script:lines += "  ${Name}: Running（等待 $($i * 3)s）"; return }
    }
    $s = Get-Service -Name $Name -ErrorAction SilentlyContinue
    $script:lines += "  ${Name}: 等待超时，当前状态 $($s.Status)"
}

# --- 1. 5330 应用服务 -------------------------------------------------------
$lines += "--- $svcApp ---"
$r1 = Ensure-Service -Name $svcApp -App $venvPy `
    -AppArgs "-m uvicorn fastgpt_assistant.server.app:app --host 0.0.0.0 --port 5330" `
    -OutLog "$root\logs\5330.out.log" -ErrLog "$root\logs\5330.err.log"
if ($r1 -ne $false) {
    & $nssm set $svcApp DisplayName "FastGpt Assistant (WeChat AI Customer Service)" 2>&1 | Out-Null
    & $nssm set $svcApp Description "Hans AI customer service on port 5330: FastAPI + GeWe callback + FastGPT knowledge base" 2>&1 | Out-Null
}

# --- 2. cpolar 隧道服务 -----------------------------------------------------
$lines += "--- $svcTunnel ---"
# 只在隧道服务没在跑的时候才清理遗留进程。cpolar 会 fork：
# 误杀持有隧道的子进程会让隧道重连、免费域名再漂一次。
$tunnelSvc = Get-Service -Name $svcTunnel -ErrorAction SilentlyContinue
if (-not $tunnelSvc -or $tunnelSvc.Status -ne "Running") {
    Get-Process cpolar -ErrorAction SilentlyContinue | ForEach-Object {
        $lines += "  结束遗留 cpolar PID=$($_.Id)"
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
} else {
    $lines += "  隧道服务已在运行，跳过进程清理（避免域名漂移）"
}

# 服务以 LocalSystem 运行，%USERPROFILE% 不是 C:\Users\THINK，
# 必须显式传配置文件绝对路径，否则读不到 authtoken 会直接退出。
$r2 = Ensure-Service -Name $svcTunnel -App "D:\tools\cpolar.exe" `
    -AppArgs "start website -log=stdout -config=$cpolarCfg" `
    -OutLog "$root\logs\cpolar.out.log" -ErrLog "$root\logs\cpolar.err.log"
if ($r2 -ne $false) {
    & $nssm set $svcTunnel DisplayName "cpolar tunnel (website -> 5330)" 2>&1 | Out-Null
    & $nssm set $svcTunnel Description "cpolar tunnel exposing local port 5330 for the GeWe WeChat callback" 2>&1 | Out-Null
}

# --- 3. 按需启动 -----------------------------------------------------------
$lines += "--- 启动 ---"
Start-Needed -Name $svcTunnel -NeedRestart $r2
Start-Needed -Name $svcApp    -NeedRestart $r1

# --- 4. 取权威公网地址（cpolar API 优先）----------------------------------
$lines += "--- 公网地址 ---"
$pub = Get-CpolarPublicUrl
$lines += "  来源: $($pub.Source)"
$lines += "  地址: $($pub.Url)"

# --- 5. 同步 .env 并重启应用让它读到新值 ----------------------------------
if ($pub.Url -ne "") {
    $lines += "--- 同步 .env ---"
    $before = ""
    $envPath = Join-Path $root ".env"
    if (Test-Path $envPath) {
        $m0 = [regex]::Match((Read-Shared $envPath), '(?m)^PUBLIC_BASE_URL=(.+)$')
        if ($m0.Success) { $before = $m0.Groups[1].Value.Trim() }
    }
    $envOut = & $venvPy (Join-Path $root "scripts\set_public_base_url.py") $pub.Url 2>&1 | Out-String
    $lines += ($envOut.Trim())
    $lines += "  变更: $before -> $($pub.Url)"

    # 应用只在启动时读 .env，必须重启才生效；重启应用不影响 cpolar 域名
    $lines += "--- 重启应用服务以加载新地址 ---"
    Restart-Service -Name $svcApp -Force -ErrorAction SilentlyContinue
    for ($i = 1; $i -le 20; $i++) {
        Start-Sleep -Seconds 3
        $s = Get-Service -Name $svcApp -ErrorAction SilentlyContinue
        if ($s -and $s.Status -eq "Running") { $lines += "  ${svcApp}: Running（等待 $($i * 3)s）"; break }
    }
    if (-not $s -or $s.Status -ne "Running") { $lines += "  !! $svcApp 重启后状态异常: $($s.Status)" }
}

# --- 6. 回读注册表校验（Unicode 安全）--------------------------------------
$lines += "--- 注册表校验 ---"
foreach ($svc in @($svcApp, $svcTunnel)) {
    $s = Get-Service $svc -ErrorAction SilentlyContinue
    if ($s) { $lines += "  $svc 状态=$($s.Status) 启动类型=$($s.StartType)" }
    else { $lines += "  $svc !! 不存在"; continue }
    $lines += "    Application   = $(Get-SvcParam $svc 'Application')"
    $lines += "    AppParameters = $(Get-SvcParam $svc 'AppParameters')"
    $lines += "    AppDirectory  = $(Get-SvcParam $svc 'AppDirectory')"
}

# --- 7. 本地 + 公网健康检查 -----------------------------------------------
$lines += "--- 本地健康检查 http://127.0.0.1:5330/health ---"
$healthy = $false
for ($i = 1; $i -le 15; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:5330/health" -TimeoutSec 10
        $lines += "  OK（第 $i 次探测）"
        foreach ($p in $h.PSObject.Properties) {
            if ($p.Value -is [string]) {
                $lines += "    $($p.Name) = $($p.Value)"
            } elseif ($p.Value -is [System.Management.Automation.PSCustomObject]) {
                $kv = $p.Value.PSObject.Properties |
                      Where-Object { $_.Name -match '^(status|ok|ready|state|error|exists|configured)$' } |
                      ForEach-Object { "$($_.Name)=$($_.Value)" }
                if ($kv) { $lines += "    $($p.Name): $($kv -join ' ')" }
            }
        }
        $healthy = $true
        break
    } catch { Start-Sleep -Seconds 4 }
}
$lines += "  healthy = $healthy"

if ($pub.Url -ne "") {
    $lines += "--- 公网端到端检查 ${pub.Url}/health ---"
    try {
        $r = Invoke-WebRequest -Uri "$($pub.Url)/health" -TimeoutSec 25 -UseBasicParsing
        $lines += "  HTTP $($r.StatusCode)"
    } catch {
        $resp = $_.Exception.Response
        if ($resp) { $lines += "  HTTP $([int]$resp.StatusCode)（隧道通、后端可能未就绪）" }
        else { $lines += "  FAIL $($_.Exception.Message)" }
    }
}

# --- 8. 每日 7 点数据同步计划任务 -----------------------------------------
$lines += "--- 计划任务 ---"
$taskName = "FastGptAssistant-DataSync"
try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$root\scripts\run_sync_bitable.ps1`"" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At 07:00
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settingsSet = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settingsSet -Force | Out-Null
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    if ($t) { $lines += "  任务: $($t.TaskName) 状态=$($t.State) 下次运行=$($info.NextRunTime)" }
    else { $lines += "  任务注册后仍查询不到" }
} catch {
    $lines += "  任务创建失败: $($_.Exception.Message)"
}

# --- 9. 收尾 ---------------------------------------------------------------
$appState = (Get-Service $svcApp -ErrorAction SilentlyContinue).Status
$lines += "--- 总体 ---"
$lines += "  5330 服务: $appState"
$lines += "  本地健康: $healthy"
$lines += "  公网地址: $($pub.Url)"
$lines += "  GeWe 回调: $($pub.Url)/api/wechat/callback"

$lines | Set-Content -Path $result -Encoding UTF8



