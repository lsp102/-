# 在云主机上运行：一键起服务 + 开防火墙 + 打印访问地址
#
# 用法（在云主机的 PowerShell 里，以管理员身份）：
#   .\deploy\cloud-windows.ps1 -Token fg2026
#   .\deploy\cloud-windows.ps1 -Token fg2026 -Stop     # 停止
#
# 前提：这台机器上已经有 FallGuard 目录，并且装了 Python 3.10+

param(
    [string]$Token = "fg2026",
    [int]$Port = 8000,
    [string]$Root = "C:\FallGuard",
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

Say "=== FallGuard 云主机部署 ===" "Cyan"
Say "目录: $Root   端口: $Port" 

# ---------------------------------------------------------------- 停止
if ($Stop) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*harness.server*" } |
        ForEach-Object {
            Say "停止进程 $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force
        }
    Say "已停止" "Green"
    return
}

# ---------------------------------------------------------------- 检查
if (-not (Test-Path (Join-Path $Root "harness\server.py"))) {
    Say "找不到 $Root\harness\server.py" "Red"
    Say "请先把整个 FallGuard 目录复制到 C:\FallGuard" "Yellow"
    exit 1
}

$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) {
    Say "这台机器没装 Python。" "Red"
    Say "装一个（3.12，勾选 Add to PATH）: https://www.python.org/downloads/release/python-3129/" "Yellow"
    exit 1
}
Say ("Python: " + (& python --version 2>&1))

# ---------------------------------------------------------------- 启动
$env:ACCESS_TOKEN   = $Token
$env:HOST           = "0.0.0.0"
$env:PORT           = "$Port"
$env:PYTHONUNBUFFERED = "1"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*harness.server*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Say "已停掉旧实例 $($_.ProcessId)" }

Start-Process -FilePath $py.Source `
    -ArgumentList "-m", "harness.server" `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $Root "server.log") `
    -RedirectStandardError  (Join-Path $Root "server.err.log")

Start-Sleep -Seconds 2

# ---------------------------------------------------------------- 防火墙
try {
    $rule = Get-NetFirewallRule -DisplayName "FallGuard $Port" -ErrorAction SilentlyContinue
    if (-not $rule) {
        New-NetFirewallRule -DisplayName "FallGuard $Port" -Direction Inbound `
            -Action Allow -Protocol TCP -LocalPort $Port | Out-Null
        Say "已添加 Windows 防火墙规则 TCP $Port" "Green"
    } else {
        Say "防火墙规则已存在"
    }
} catch {
    Say "防火墙规则添加失败（可能不是管理员）: $_" "Yellow"
}

# ---------------------------------------------------------------- 自检
try {
    $h = Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 5
    Say "本机自检通过: healthz ok=$($h.ok) active=$($h.active)" "Green"
} catch {
    Say "本机自检失败，看 server.err.log" "Red"
    Get-Content (Join-Path $Root "server.err.log") -ErrorAction SilentlyContinue | Select-Object -Last 10
    exit 1
}

# ---------------------------------------------------------------- 公网地址
$pub = $null
foreach ($u in @("https://api.ipify.org", "https://ifconfig.me/ip", "https://ipinfo.io/ip")) {
    try {
        $pub = (Invoke-RestMethod $u -TimeoutSec 6).ToString().Trim()
        if ($pub) { break }
    } catch { }
}

Say ""
Say "=== 完成 ==="  "Cyan"
if ($pub) {
    Say "  http://${pub}:${Port}/?token=$Token" "Green"
} else {
    Say "  公网 IP 查询失败，请手动拼接：http://<你的公网IP>:$Port/?token=$Token" "Yellow"
}
Say ""
Say "如果外网打不开，去腾讯云控制台放行 TCP $Port：" "Yellow"
Say "  轻量应用服务器 → 服务器详情 → 防火墙 → 添加规则"
Say "  云服务器 CVM   → 安全组 → 入站规则 → 添加 TCP:$Port"
