# install.ps1 - pasloe-screenshot 安装脚本
# 用法: .\install.ps1 [-ConfigPath <path>]
param(
    [string]$ConfigPath = "$env:APPDATA\pasloe-screenshot\config.toml"
)

$ErrorActionPreference = "Stop"
$TaskName = "PasloeScreenshot"
$BinName = "pasloe-screenshot.exe"

Write-Host "==> 构建 pasloe-screenshot..."
cargo build --release --quiet
if ($LASTEXITCODE -ne 0) { exit 1 }

# 安装二进制
Write-Host "==> 安装二进制..."
cargo install --path . --quiet
if ($LASTEXITCODE -ne 0) { exit 1 }

# 解析实际二进制路径
$CargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { "$env:USERPROFILE\.cargo" }
$BinPath = Join-Path $CargoHome "bin\$BinName"

if (-not (Test-Path $BinPath)) {
    Write-Error "安装失败：找不到 $BinPath"
    exit 1
}

# 初始化配置文件
$ConfigDir = Split-Path $ConfigPath -Parent
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    Write-Host "==> 创建配置目录: $ConfigDir"
}

if (-not (Test-Path $ConfigPath)) {
    Copy-Item "config\config.example.toml" $ConfigPath
    Write-Host "==> 已复制示例配置到: $ConfigPath"
    Write-Host "    请编辑配置文件后再启动服务"
} else {
    Write-Host "==> 配置文件已存在，跳过: $ConfigPath"
}

# 注册计划任务
$Existing = schtasks /query /tn $TaskName 2>$null
if ($Existing) {
    Write-Host "==> 任务已存在，先删除旧任务..."
    schtasks /delete /tn $TaskName /f | Out-Null
}

$Action = "`"$BinPath`" capture --config `"$ConfigPath`""
schtasks /create /tn $TaskName /tr $Action /sc onlogon /ru $env:USERNAME /rl limited /f | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "注册计划任务失败"
    exit 1
}

Write-Host ""
Write-Host "安装完成。"
Write-Host "  二进制: $BinPath"
Write-Host "  配置:   $ConfigPath"
Write-Host "  任务:   $TaskName (登录时自动启动)"
Write-Host ""
Write-Host "立即启动:"
Write-Host "  schtasks /run /tn $TaskName"
Write-Host "查看运行状态:"
Write-Host "  schtasks /query /tn $TaskName /fo list"
