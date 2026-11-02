<#
.SYNOPSIS
  用无头 Edge 把控制台的场景渲染成 PNG，产物放到 shots/。

.DESCRIPTION
  为什么不用“截图真实窗口”：Windows 上 PrintWindow 要求进程 DPI 感知，
  否则只会抓到窗口左侧一块（2560 宽的窗口被画进 1721 的位图里）。
  无头模式没有这个问题，而且可重复、可进 CI。

  注意：本文件必须以 UTF-8 with BOM 保存，否则 PowerShell 5.1 会按 ANSI 读中文。

.EXAMPLE
  powershell -File tools/shot.ps1
  powershell -File tools/shot.ps1 -Port 8010 -Width 1400 -Height 900
  powershell -File tools/shot.ps1 -Only s11
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [int]$Width = 1600,
    [int]$Height = 1000,
    [int]$WaitMs = 25000,
    [string]$Only = "",
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root "shots" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$candidates = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$browser = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) { throw "没有找到 Edge / Chrome，无法渲染" }

$base = "http://127.0.0.1:$Port"
try {
    Invoke-RestMethod -Uri "$base/api/packs" -TimeoutSec 5 | Out-Null
} catch {
    throw "控制台未启动：先跑 python -m harness.server $Port"
}

$shots = @(
    @{ Name = "console-fall-response"; Query = "pack=fall-response&run=s13&pace=0" },
    @{ Name = "console-phone-drop";    Query = "pack=fall-response&run=s11&pace=0" },
    @{ Name = "console-research-brief"; Query = "pack=research-brief&run=r1&pace=0" }
)
if ($Only) { $shots = $shots | Where-Object { $_.Name -like "*$Only*" -or $_.Query -like "*$Only*" } }
if (-not $shots) { throw "没有匹配 -Only '$Only' 的场景" }

$sizeArg = "--window-size=$Width,$Height"
$budgetArg = "--virtual-time-budget=$WaitMs"

foreach ($s in $shots) {
    $out = Join-Path $OutDir "$($s.Name).png"
    $url = "$base/?$($s.Query)"
    Write-Host "-> $($s.Name)"

    # Edge 会往 stderr 写无害告警；PS 5.1 在 Stop 模式下会把它当异常
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $browser --headless=new --disable-gpu --hide-scrollbars `
        --force-device-scale-factor=1 $sizeArg $budgetArg `
        "--screenshot=$out" $url 2>$null | Out-Null
    $ErrorActionPreference = $prevEap

    if (-not (Test-Path $out)) { throw "渲染失败：$($s.Name)" }
    $kb = [math]::Round((Get-Item $out).Length / 1KB, 1)
    Write-Host "   $out  ($kb KB)"
}

Write-Host "完成：$($shots.Count) 张，目录 $OutDir"
