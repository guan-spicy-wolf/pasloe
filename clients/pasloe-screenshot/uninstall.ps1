# uninstall.ps1 - pasloe-screenshot 卸载脚本
# 用法: .\uninstall.ps1 [-KeepConfig] [-KeepBin]
param(
    [switch]$KeepConfig,
    [switch]$KeepBin
)

$ErrorActionPreference = "Stop"
$TaskName = "PasloeScreenshot"
$ConfigDir = "$env:APPDATA\pasloe-screenshot"
$CargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { "$env:USERPROFILE\.cargo" }

# 停止并删除计划任务
$Existing = schtasks /query /tn $TaskName 2>$null
if ($Existing) {
    Write-Host "==> 停止并删除计划任务 $TaskName..."
    schtasks /end /tn $TaskName 2>$null | Out-Null
    schtasks /delete /tn $TaskName /f | Out-Null
} else {
    Write-Host "==> 计划任务不存在，跳过"
}

# 卸载二进制
if (-not $KeepBin) {
    Write-Host "==> 卸载二进制..."
    cargo uninstall pasloe-screenshot --quiet 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    二进制不存在或已卸载，跳过"
    }
} else {
    Write-Host "==> 保留二进制（-KeepBin）"
}

# 删除配置目录
if (-not $KeepConfig) {
    if (Test-Path $ConfigDir) {
        Write-Host "==> 删除配置目录: $ConfigDir"
        Remove-Item $ConfigDir -Recurse -Force
    } else {
        Write-Host "==> 配置目录不存在，跳过"
    }
} else {
    Write-Host "==> 保留配置目录（-KeepConfig）: $ConfigDir"
}

Write-Host ""
Write-Host "卸载完成。"
