# Run as administrator. Opens only the specified LAN, then reloads the service.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Network,
    [Parameter(Mandatory = $true)][string]$LocalAddress
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$lanAccessLog = Join-Path $projectRoot 'logs/enable-lan-access.log'
$adminPrincipal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $adminPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script in administrator PowerShell.'
}
if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LocalAddress -ErrorAction SilentlyContinue)) {
    throw 'The specified local address is not assigned to this computer.'
}
New-Item -ItemType Directory -Path (Join-Path $projectRoot 'logs') -Force | Out-Null
Start-Transcript -Path $lanAccessLog -Append | Out-Null
try {
    & (Join-Path $projectRoot '.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'configure_lan_access.py') --network $Network
    if ($LASTEXITCODE -ne 0) { throw 'LAN configuration failed; firewall not changed.' }
    $ruleName = 'FastGptAssistant-LAN-TCP-5330'
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($rule) {
        $rule | Set-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -Profile Any
        $rule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort 5330
        $rule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -LocalAddress $LocalAddress -RemoteAddress $Network
    } else {
        New-NetFirewallRule -Name $ruleName -DisplayName 'FastGPT Assistant LAN TCP 5330' -Enabled True -Direction Inbound -Action Allow -Profile Any -Protocol TCP -LocalPort 5330 -LocalAddress $LocalAddress -RemoteAddress $Network | Out-Null
    }
    Restart-Service -Name FastGptAssistant -ErrorAction Stop
    $healthy = $false
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 2
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:5330/health' -TimeoutSec 3
            if ($health.status -eq 'healthy') { $healthy = $true; break }
        } catch { }
    }
    if (-not $healthy) { throw 'Service restarted but health check failed.' }
    Write-Output "LAN access ready: http://${LocalAddress}:5330/admin/ ($Network)"
} finally {
    Stop-Transcript | Out-Null
}
