<#
.SYNOPSIS
  把本机 5330 服务通过 frp（HTTPS vhost）接入公网，并注册为 Windows 服务常驻。

.DESCRIPTION
  做五件事：确保 frpc.exe 就位 → 部署证书 → 由模板渲染 frpc.toml → 预检配置 → 幂等注册/启动服务 → 探测公网 /health。
  本脚本**不碰 cpolar**，可与之并行运行，便于随时回退。

  证书在**本机**终结（插件 https2http），frps 只按 SNI 转发字节流，服务器端不需要证书。

.PARAMETER ProxyMode
  业务代理的 TLS 终结位置：
    http  （默认）TLS 由服务器侧 OpenResty/Nginx 终结（如 1Panel 反向代理站点）→ 本机**不需要证书**，
           frps 需把 vhostHTTPPort 改成 8080（80 被 OpenResty 占了）。
    https TLS 在本机 frpc 终结（https2http 插件）→ 需要证书，且 frps 必须独占 443。

.PARAMETER LocalPort
  本机被代理的服务端口，默认 5330。

.PARAMETER InputFile
  一次性读入所有参数（KEY=VALUE 纯文本），避免把 token 写进命令行/历史。
  支持的键：SERVER_ADDR SERVER_PORT FRP_TOKEN TOKEN_FILE DOMAIN CERT_FILE KEY_FILE
            PUBLIC_HTTPS_PORT FRP_USER SERVICE_NAME PROXY_MODE LOCAL_PORT
  规则：`#` 开头是注释行；**不支持行内注释**（值里可能含 #），值不要加引号。
  命令行上显式给出的参数优先级高于本文件。

.PARAMETER ServerAddr
  frps 所在公网服务器的域名或 IP。

.PARAMETER Token
  frps 的 auth.token。注意：直接写在命令行会进 PowerShell 历史；更稳妥的是 -TokenFile 或 -InputFile。

.PARAMETER TokenFile
  从文件读取 token（首行非空内容），优先于 -Token。

.PARAMETER Domain
  对外使用的固定域名，例如 bot.example.com。必须已解析到 frps 服务器。

.PARAMETER CertPath / KeyPath
  证书全链与私钥。提供后会复制为 <InstallDir>\certs\fullchain.pem / privkey.pem。
  不提供则要求这两个文件已就位。

.PARAMETER FrpUser
  本客户端标识，同一 frps 下多个客户端不能重名。默认取本机机器名小写。

.EXAMPLE
  # 推荐：把值填进 %USERPROFILE%\.frp\input.txt，然后用这一条命令完成安装（管理员）
  .\setup_frpc.ps1 -InputFile "$env:USERPROFILE\.frp\input.txt"

.EXAMPLE
  # 只渲染配置并预检，不注册服务、不需要管理员
  .\setup_frpc.ps1 -ServerAddr 1.2.3.4 -Domain bot.example.com -TokenFile C:\secret\token.txt -ConfigOnly

.EXAMPLE
  # 完整安装（管理员 PowerShell 执行）
  .\setup_frpc.ps1 -ServerAddr 1.2.3.4 -Domain bot.example.com -TokenFile C:\secret\token.txt `
      -CertPath D:\certs\fullchain.pem -KeyPath D:\certs\privkey.pem

.EXAMPLE
  # 隧道验通后才切 .env 并重启应用（切 GeWe 回调仍需在 GeWe 控制台手工做）
  .\setup_frpc.ps1 -InputFile "$env:USERPROFILE\.frp\input.txt" -UpdateEnv
#>
[CmdletBinding()]
param(
    [string]$InputFile = "",
    [string]$ServerAddr = "",
    [int]$ServerPort = 7000,
    [string]$Token = "",
    [string]$TokenFile = "",
    [string]$Domain = "",
    [string]$CertPath = "",
    [string]$KeyPath = "",
    [int]$PublicHttpsPort = 443,
    [string]$ProxyMode = "",
    [int]$LocalPort = 0,
    [string]$FrpUser = "",
    [string]$AdminPwd = "",
    [string]$InstallDir = "D:\tools\frp",
    [string]$ServiceName = "frpc-wechat",
    [string]$NssmPath = "D:\tools\nssm-2.24\win64\nssm.exe",
    [string]$FrpVersion = "0.71.0",
    [switch]$ConfigOnly,
    [switch]$UpdateEnv
)

$ErrorActionPreference = "Continue"

$script:Step = 0
function Step([string]$text) {
    $script:Step++
    Write-Host ""
    Write-Host "=== $script:Step. $text ===" -ForegroundColor Cyan
}
function Fail([string]$text) {
    # 控制台标记一律用 ASCII：中文 Windows 的控制台代码页是 GBK，
    # ✓/✗/⚠ 这类字符在 GBK 里没有对应码位，Write-Host 会退化成 "?"，很难看也难排查。
    Write-Host "[FAIL] $text" -ForegroundColor Red
    exit 1
}
function Ok([string]$text) {
    Write-Host "[OK] $text" -ForegroundColor Green
}
function Note([string]$text) {
    Write-Host "[注意] $text" -ForegroundColor Yellow
}

$root = Split-Path -Parent $PSScriptRoot
$template = Join-Path $root "deploy\frpc\frpc.toml.example"
$configPath = Join-Path $InstallDir "frpc.toml"
$frpcExe = Join-Path $InstallDir "frpc.exe"
$certDir = Join-Path $InstallDir "certs"
$crtDest = Join-Path $certDir "fullchain.pem"
$keyDest = Join-Path $certDir "privkey.pem"
$logFile = (Join-Path $InstallDir "frpc.log").Replace("\", "/")

# --------------------------------------------------------------------------
# 从 -InputFile 读入参数（在参数校验之前）
# --------------------------------------------------------------------------
if ($InputFile) {
    if (-not (Test-Path $InputFile)) { Fail "InputFile 不存在: $InputFile" }
    $kv = @{}
    foreach ($line in [IO.File]::ReadAllLines($InputFile, [Text.Encoding]::UTF8)) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $kv[$t.Substring(0, $i).Trim().ToUpper()] = $t.Substring($i + 1).Trim()
    }
    # 命令行显式给出的参数优先；没给的才由文件填
    $map = @{
        ServerAddr = "SERVER_ADDR"; ServerPort = "SERVER_PORT"; Token = "FRP_TOKEN"
        TokenFile = "TOKEN_FILE"; Domain = "DOMAIN"; CertPath = "CERT_FILE"
        KeyPath = "KEY_FILE"; PublicHttpsPort = "PUBLIC_HTTPS_PORT"
        FrpUser = "FRP_USER"; ServiceName = "SERVICE_NAME"
        ProxyMode = "PROXY_MODE"; LocalPort = "LOCAL_PORT"
    }
    foreach ($pname in $map.Keys) {
        if ($PSBoundParameters.ContainsKey($pname)) { continue }
        $key = $map[$pname]
        if (-not $kv.ContainsKey($key) -or -not $kv[$key]) { continue }
        $v = $kv[$key]
        if ($pname -in @("ServerPort", "PublicHttpsPort", "LocalPort")) { $v = [int]$v }
        Set-Variable -Name $pname -Value $v -Scope 0
    }
    Ok "已读入 $InputFile"
}

# 默认值（注意：LocalPort 用 0 作"未指定"哨兵，别写 $LocalPort = 5330 然后判断是否被绑定）
if (-not $ProxyMode) { $ProxyMode = "http" }
$ProxyMode = $ProxyMode.Trim().ToLower()
if ($ProxyMode -notin @("http", "https")) { Fail "ProxyMode 只能是 http 或 https，收到: $ProxyMode" }
if ($LocalPort -le 0) { $LocalPort = 5330 }

# --------------------------------------------------------------------------
Step "参数与权限检查"
# --------------------------------------------------------------------------
if (-not $ServerAddr) { Fail "缺少 frps 服务器地址（-ServerAddr 或 -InputFile 里的 SERVER_ADDR）" }
if (-not $Domain) { Fail "缺少对外域名（-Domain 或 -InputFile 里的 DOMAIN）" }
if ($Domain -notmatch "^[0-9A-Za-z\-]+(\.[0-9A-Za-z\-]+)+$") {
    Fail "Domain 看起来不像域名: $Domain（type=https 只按 TLS SNI 路由，必须用域名，不能用 IP）"
}
if ($PublicHttpsPort -ne 443) {
    Write-Host "  注意：公网 HTTPS 端口非 443（$PublicHttpsPort），回调地址须带端口。"
}
if (-not (Test-Path $template)) { Fail "找不到配置模板: $template" }

$elevated = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $ConfigOnly -and -not $elevated) {
    Fail "注册/启动 Windows 服务需要管理员权限。请以管理员身份打开 PowerShell 重跑，或加 -ConfigOnly 只渲染配置。"
}
Write-Host ("  权限: " + $(if ($elevated) { "已提权" } else { "未提权（-ConfigOnly 模式，可继续）" }))

if ($TokenFile) {
    if (-not (Test-Path $TokenFile)) { Fail "TokenFile 不存在: $TokenFile" }
    $Token = (Get-Content -Path $TokenFile -Encoding UTF8 | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
}
if (-not $Token) { Fail "缺少 token。用 -Token 或 -TokenFile 提供。" }
Ok "token 已就位（长度 $($Token.Length)，不回显）"

# 本客户端标识：默认机器名；同一 frps 下不能与其他客户端重名
if (-not $FrpUser) {
    $FrpUser = ($env:COMPUTERNAME).ToLower()
    if (-not $FrpUser) { $FrpUser = "windows-client-01" }
}
if ($FrpUser -notmatch "^[0-9A-Za-z\-_]{1,32}$") {
    Fail "FrpUser 只允许字母/数字/连字符/下划线，且不超过 32 字符: $FrpUser"
}
Ok "客户端标识: $FrpUser"

# frpc 本机管理接口密码（127.0.0.1:7400，仅本机可达）
if (-not $AdminPwd) {
    $bytes = New-Object byte[] 16
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $AdminPwd = -join ($bytes | ForEach-Object { "{0:x2}" -f $_ })
}

# --------------------------------------------------------------------------
Step "确保 frpc.exe 就位"
# --------------------------------------------------------------------------
if (Test-Path $frpcExe) {
    Ok "已存在: $frpcExe"
} else {
    $zipName = "frp_" + $FrpVersion + "_windows_amd64"
    $zipPath = Join-Path "D:\tools" ($zipName + ".zip")
    $urlPath = "fatedier/frp/releases/download/v" + $FrpVersion + "/" + $zipName + ".zip"
    New-Item -ItemType Directory -Force -Path "D:\tools" | Out-Null
    # 镜像顺序实测：gh-proxy.com 快且完整；ghproxy.net 曾返回截断包（约 8MB，缺中央目录），放最后
    $mirrors = @("https://gh-proxy.com/", "https://ghfast.top/", "https://hub.gitmirror.com/", "https://ghproxy.net/")
    $got = $false
    foreach ($m in $mirrors) {
        if ($got) { break }
        $u = $m + "github.com/" + $urlPath
        Write-Host "  尝试: $m"
        try {
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest -Uri $u -OutFile $zipPath -TimeoutSec 600
            # 完整性校验：必须能作为 zip 打开且含 frpc.exe，否则换下一个镜像重试
            $names = @()
            try {
                Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
                $z = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
                $names = @($z.Entries | ForEach-Object { $_.FullName })
                $z.Dispose()
            } catch {
                Write-Host "    压缩包不完整/不可读，换下一个镜像"
                continue
            }
            if (-not ($names | Where-Object { $_ -like "*frpc.exe" })) {
                Write-Host "    包内没有 frpc.exe，换下一个镜像"
                continue
            }
            $got = $true
            Ok ("下载完成 " + [math]::Round((Get-Item $zipPath).Length / 1MB, 2) + " MB，已校验含 frpc.exe")
        } catch {
            Write-Host ("    失败: " + $_.Exception.Message.Substring(0, [Math]::Min(60, $_.Exception.Message.Length)))
        }
    }
    if (-not $got) {
        Fail "所有下载源都失败。请手动下载 frp_${FrpVersion}_windows_amd64.zip 并解压到 $InstallDir"
    }
    $tmp = Join-Path "D:\tools" "_frp_unpack"
    Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
    $inner = Join-Path $tmp $zipName
    if (-not (Test-Path $inner)) { Fail "解压后未找到 $inner" }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item (Join-Path $inner "*") $InstallDir -Recurse -Force
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Ok "已安装到 $InstallDir"
}
$verLine = (& $frpcExe -v 2>&1 | Out-String).Trim()
Ok "frpc 版本: $verLine"

# --------------------------------------------------------------------------
if ($ProxyMode -eq "https") {
    Step "准备证书（TLS 在本机终结）"
    # --------------------------------------------------------------------------
    New-Item -ItemType Directory -Force -Path $certDir | Out-Null
    if ($CertPath -or $KeyPath) {
        if (-not (Test-Path $CertPath)) { Fail "证书不存在: $CertPath" }
        if (-not (Test-Path $KeyPath)) { Fail "私钥不存在: $KeyPath" }
        Copy-Item $CertPath $crtDest -Force
        Copy-Item $KeyPath $keyDest -Force
        Ok "证书已复制到 $certDir"
    } elseif ((Test-Path $crtDest) -and (Test-Path $keyDest)) {
        Ok "使用已就位证书: $certDir"
    } else {
        Write-Host ""
        Write-Host "缺少证书。请在 $certDir 放入 fullchain.pem 与 privkey.pem，或加 -CertPath / -KeyPath。" -ForegroundColor Yellow
        Write-Host "获取方式：① 云厂商免费 DV 证书（DNS 验证，最省事）② win-acme 申请 Let's Encrypt ③ 已有商业证书"
        Write-Host "注意：证书主体必须是 $Domain，否则 GeWe 与浏览器会校验证书失败。"
        Fail "证书未就绪"
    }
} else {
    Step "证书（PROXY_MODE=http，本机不需要证书）"
    Write-Host "  TLS 由服务器侧 OpenResty/Nginx 终结，本机只跑明文 http，跳过证书步骤。"
    Write-Host "  [注意] 前提：frps 的 vhostHTTPPort 必须改成 8080（80 已被 OpenResty 占用）。"
}

# --------------------------------------------------------------------------
Step "渲染 frpc.toml（PROXY_MODE=$ProxyMode）"
# --------------------------------------------------------------------------
$text = Get-Content -Path $template -Raw -Encoding UTF8

# 业务代理段按模式注入：TOML 对表顺序敏感，整段替换比在主模板里写死更安全
$blockFile = Join-Path $root ("deploy\frpc\proxies\" + $(if ($ProxyMode -eq "https") { "https-local-cert.toml" } else { "http-behind-proxy.toml" }))
if (-not (Test-Path $blockFile)) { Fail "找不到代理段模板: $blockFile" }
$block = Get-Content -Path $blockFile -Raw -Encoding UTF8
# 断言占位符只出现一次：模板注释里若也写了这个字符串，Replace 会把内容注入到注释中间，
# 导致 TOML 结构整体错位（现象是 verify 报 unknown field "auth" / 表归属错误）
$markerCount = ([regex]::Matches($text, [regex]::Escape("__PROXY_BLOCK__"))).Count
if ($markerCount -ne 1) {
    Fail "主模板里 __PROXY_BLOCK__ 出现 $markerCount 次，必须恰好 1 次: $template"
}
$text = $text.Replace("__PROXY_BLOCK__", $block.TrimEnd())

$text = $text.Replace("__SERVER_ADDR__", $ServerAddr)
$text = $text.Replace("__SERVER_PORT__", [string]$ServerPort)
$text = $text.Replace("__TOKEN__", $Token)
$text = $text.Replace("__FRP_USER__", $FrpUser)
$text = $text.Replace("__ADMIN_PWD__", $AdminPwd)
$text = $text.Replace("__DOMAIN__", $Domain)
$text = $text.Replace("__LOCAL_PORT__", [string]$LocalPort)
$text = $text.Replace("__CERT_CRT__", $crtDest.Replace("\", "/"))
$text = $text.Replace("__CERT_KEY__", $keyDest.Replace("\", "/"))
$text = $text.Replace("__LOG_FILE__", $logFile)
if ($text -match "__[A-Z_]+__") {
    Fail ("模板仍有未替换占位符: " + ($Matches[0]))
}
# TOML 不能带 BOM，用无 BOM UTF-8 写
[IO.File]::WriteAllText($configPath, $text, (New-Object System.Text.UTF8Encoding($false)))
Ok "已写出 $configPath（内容含 token，勿外传）"

# --------------------------------------------------------------------------
Step "预检配置（frpc verify）"
# --------------------------------------------------------------------------
$verify = (& $frpcExe verify -c $configPath 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    Write-Host $verify -ForegroundColor Red
    Fail "frpc verify 失败，配置有误"
}
Ok "配置语法通过"

if ($ConfigOnly) {
    Step "完成（-ConfigOnly）"
    Write-Host "  配置已渲染，服务未注册。正式安装请以管理员身份重跑本脚本（去掉 -ConfigOnly）。"
    exit 0
}

# --------------------------------------------------------------------------
Step "幂等注册 Windows 服务（$ServiceName）"
# --------------------------------------------------------------------------
if (-not (Test-Path $NssmPath)) { Fail "找不到 nssm: $NssmPath" }
$appArgs = "-c `"$configPath`""
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "  服务已存在，改为更新参数（不删除，避开 SCM 竞态）"
    & $NssmPath set $ServiceName Application $frpcExe | Out-Null
} else {
    $installed = $false
    for ($i = 0; $i -lt 6; $i++) {
        & $NssmPath install $ServiceName $frpcExe | Out-Null
        if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) { $installed = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $installed) { Fail "nssm install 失败（连续 6 次）" }
}
# 显式落库，绝不依赖 install 传参（形参名别用 $Args —— 它是自动变量，会静默吞值）
& $NssmPath set $ServiceName AppDirectory $InstallDir | Out-Null
& $NssmPath set $ServiceName AppParameters $appArgs | Out-Null
& $NssmPath set $ServiceName AppStdout (Join-Path $InstallDir "svc.out.log") | Out-Null
& $NssmPath set $ServiceName AppStderr (Join-Path $InstallDir "svc.err.log") | Out-Null
& $NssmPath set $ServiceName AppRotateFiles 1 | Out-Null
& $NssmPath set $ServiceName AppRotateBytes 10485760 | Out-Null
& $NssmPath set $ServiceName AppExit Default Restart | Out-Null
& $NssmPath set $ServiceName Start SERVICE_AUTO_START | Out-Null
Ok "服务参数已写入"

# --------------------------------------------------------------------------
Step "启动服务并等待 Running"
# --------------------------------------------------------------------------
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc.Status -eq "Running") {
    Restart-Service -Name $ServiceName -Force
} else {
    Start-Service -Name $ServiceName
}
$running = $false
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") { $running = $true; Ok "服务 Running（等待 $($i * 3)s）"; break }
}
if (-not $running) { Fail "服务未能在 30s 内进入 Running，请查 $InstallDir\svc.err.log" }

# --------------------------------------------------------------------------
Step "探测公网链路"
# --------------------------------------------------------------------------
$portSuffix = $(if ($PublicHttpsPort -eq 443) { "" } else { ":" + $PublicHttpsPort })
$healthUrl = "https://" + $Domain + $portSuffix + "/health"
$probeOk = $false
for ($i = 1; $i -le 6; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 20 -UseBasicParsing
        Write-Host ("  HTTP " + $r.StatusCode + " " + $r.Content.Substring(0, [Math]::Min(160, $r.Content.Length)))
        $probeOk = $true
        break
    } catch {
        Write-Host ("  第 $i 次失败: " + $_.Exception.Message.Substring(0, [Math]::Min(70, $_.Exception.Message.Length)))
        Start-Sleep -Seconds 4
    }
}

# --------------------------------------------------------------------------
Step "结果"
# --------------------------------------------------------------------------
$callbackUrl = $healthUrl.Replace("/health", "/api/wechat/callback")
Write-Host "  服务名        : $ServiceName"
Write-Host "  配置文件      : $configPath"
Write-Host "  GeWe 回调地址 : $callbackUrl"
if ($probeOk) {
    Write-Host ""
    Ok "公网 /health 已通。frp 链路验证通过。"
} else {
    Write-Host ""
    Write-Host "公网 /health 未通。排查顺序：" -ForegroundColor Yellow
    Write-Host "  1) frps 是否开启了 vhostHTTPSPort（默认 443）"
    Write-Host "  2) $Domain 的 A 记录是否指向 frps 服务器，服务器防火墙是否放行 443"
    Write-Host "  3) 证书主体是否为 $Domain（自签证书 GeWe 会拒）"
    Write-Host "  4) 日志: $logFile 与 $(Join-Path $InstallDir 'svc.err.log')"
}
Write-Host ""
Write-Host "注意：本脚本未触碰 cpolar。切换顺序建议 ——" -ForegroundColor Cyan
Write-Host "  ① 先去 GeWe 控制台把回调改成上面的地址，发一条真实微信消息验证"
Write-Host "  ② 验证 OK 后再停 cpolar:  Stop-Service cpolar-website; Set-Service cpolar-website -StartupType Disabled"
Write-Host "  ③ 回退：把 GeWe 回调改回 cpolar 地址 + Start-Service cpolar-website 即可"
Write-Host "      （cpolar 免费域名重启后会漂，回退前用 scripts\get_public_url.py 取当前域名）"

if ($UpdateEnv) {
    Step "更新 .env 并重启应用"
    $py = Join-Path $root ".venv\Scripts\python.exe"
    $setter = Join-Path $root "scripts\set_public_base_url.py"
    if (-not (Test-Path $py)) { Fail "找不到 $py" }
    $baseUrl = "https://" + $Domain + $portSuffix
    & $py $setter $baseUrl
    if ($LASTEXITCODE -ne 0) { Fail "set_public_base_url.py 执行失败" }
    Ok ".env 已指向 $baseUrl"
    $restarter = Join-Path $root "scripts\restart_app.ps1"
    if (Test-Path $restarter) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $restarter
    } else {
        Write-Host "  未找到 restart_app.ps1，请手动: Restart-Service FastGptAssistant -Force"
    }
    Write-Host ""
    Note "别忘了在 GeWe 控制台把回调地址改成: $callbackUrl"
}

exit 0
