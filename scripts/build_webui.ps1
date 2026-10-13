# Build the admin frontend without requiring pnpm/node on the user's PATH.
[CmdletBinding()]
param(
    [string]$PnpmPath = "",
    [string]$NodePath = ""
)

$ErrorActionPreference = "Stop"
$webuiRoot = Join-Path (Split-Path -Parent $PSScriptRoot) "webui"
$bundledRuntime = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies"
$previousPath = $env:PATH

function Resolve-WebuiTool {
    param([string]$ExplicitPath, [string[]]$CommandNames, [string]$BundledPath)
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "Tool not found: $ExplicitPath"
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }
    foreach ($commandName in $CommandNames) {
        $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    if (Test-Path -LiteralPath $BundledPath -PathType Leaf) { return $BundledPath }
    throw "Missing $($CommandNames[0]). Install Node.js 22.12+ and pnpm 11, or pass -NodePath and -PnpmPath."
}

try {
    $webuiNode = Resolve-WebuiTool $NodePath @("node.exe") (Join-Path $bundledRuntime "node\bin\node.exe")
    $env:PATH = (Split-Path -Parent $webuiNode) + [IO.Path]::PathSeparator + $previousPath
    $webuiPnpm = Resolve-WebuiTool $PnpmPath @("pnpm.cmd", "pnpm.exe") (Join-Path $bundledRuntime "bin\fallback\pnpm.cmd")
    Push-Location -LiteralPath $webuiRoot
    try {
        Write-Host "Installing frontend dependencies..."
        & $webuiPnpm install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw "pnpm install failed (exit $LASTEXITCODE)." }
        Write-Host "Building admin frontend..."
        & $webuiPnpm build
        if ($LASTEXITCODE -ne 0) { throw "pnpm build failed (exit $LASTEXITCODE)." }
        Write-Host "Build complete. See docs/ for first-time setup or the required service restart."
    } finally {
        Pop-Location
    }
} finally {
    $env:PATH = $previousPath
}
