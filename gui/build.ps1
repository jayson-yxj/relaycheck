<#
.SYNOPSIS
    打包 relaycheck 的 Windows 桌面版：一个目录 + 两个 exe。

.DESCRIPTION
    在仓库根目录建一个独立的 .venv-build（不碰你平时的环境），装好 requests 和
    pyinstaller，然后按 gui\relaycheck_gui.spec 构建。

    产物：dist\relaycheck-desktop\
        relaycheck-gui.exe   窗口程序，给人双击的
        relaycheck.exe       控制台程序，GUI 只用它来跑审计
        _internal\           运行时、tcl/tk、requests —— 必须跟着一起拷

.PARAMETER Python
    用来建构建 venv 的解释器。省略时自动探测（py -3.x → python → conda 基础环境）。
    探测会同时要求 tkinter 可用、版本 >= 3.9，否则 PyInstaller 打不出窗口程序。

.PARAMETER NoClean
    保留 PyInstaller 的缓存和上次的 build 中间产物。默认每次都 --clean。

.PARAMETER Test
    构建完成后跑 gui\e2e_bundle.py：用本地 mock 中转站把三个 runner（源码 CLI、
    引擎 exe、窗口 exe 的 --run-audit 通道）跑同一份审计并逐字段比对结论。

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Python 'C:\Python312\python.exe' -Test
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$NoClean,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir  = Join-Path $RepoRoot '.venv-build'
$VenvPy   = Join-Path $VenvDir 'Scripts\python.exe'
$SpecPath = Join-Path $PSScriptRoot 'relaycheck_gui.spec'
$OutDir   = Join-Path $RepoRoot 'dist\relaycheck-desktop'

if (-not (Test-Path -LiteralPath $SpecPath)) {
    throw "找不到 spec：$SpecPath"
}

# 没有 tkinter 的 Python 能装 PyInstaller、能构建，然后在 EXE(console=False) 那一步
# 失败并给出一条和 tkinter 毫无关系的报错。所以在建 venv 之前就把这个否掉。
$Probe = 'import sys, tkinter; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 3)'

# 原生程序往 stderr 写一句无害 WARNING 不该让构建失败。但 $ErrorActionPreference='Stop' 碰上
# 调用方做了 2>&1（`.\build.ps1 2>&1 | Tee-Object build.log` 这种再正常不过的写法）就会把
# stderr 升级成 NativeCommandError 终止错误 —— PyInstaller 用 conda 解释器时每次都打一句
# conda-meta 警告，所以这条必定踩得到，而且报错信息里只有那句 WARNING，看不出跟构建有什么关系。
# 退出码该查还是逐条查，只是不再由 stderr 决定生死。
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$Quiet)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) {
            $null = & $Exe @Arguments 2>&1
        } else {
            & $Exe @Arguments
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-CandidateInterpreters {
    $list = New-Object System.Collections.ArrayList
    if ($Python) {
        [void]$list.Add(@($Python))
        return $list
    }
    foreach ($v in @('3.13', '3.12', '3.11', '3.10', '3.9')) {
        [void]$list.Add(@('py', "-$v"))
    }
    [void]$list.Add(@('py', '-3'))
    [void]$list.Add(@('python'))
    [void]$list.Add(@('python3'))
    if ($env:CONDA_PREFIX) {
        $condaPy = Join-Path $env:CONDA_PREFIX 'python.exe'
        if ($env:CONDA_PREFIX -notmatch '[\u4e00-\u9fff]') { [void]$list.Add(@($condaPy)) }
    }
    return $list
}

Write-Host '== 找一个能用的解释器 ==' -ForegroundColor Cyan
$base = $null
foreach ($cand in Get-CandidateInterpreters) {
    $exe = $cand[0]
    $pre = @()
    if ($cand.Count -gt 1) { $pre = $cand[1..($cand.Count - 1)] }

    $shown = (@($exe) + $pre) -join ' '
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $exe)) {
        continue
    }

    # 版本不匹配时 py 会写 stderr 并返回非 0；tkinter 缺失时抛 ImportError。两种都算不合适。
    $code = Invoke-Native $exe ($pre + @('-c', $Probe)) -Quiet
    if ($code -eq 0) {
        $base = $cand
        Write-Host "   用：$shown" -ForegroundColor Green
        break
    }
    Write-Host "   跳过：$shown (exit $code)" -ForegroundColor DarkGray
}

if (-not $base) {
    throw @'
没找到可用解释器。要求 Python >= 3.9 并且带 tkinter。

  * python.org 的官方安装包默认带 tkinter，勾上 "tcl/tk and IDLE" 即可。
  * conda 基础环境通常也带。
  * 手动指定：.\build.ps1 -Python 'C:\Python312\python.exe'
'@
}

$exe = $base[0]
$pre = @()
if ($base.Count -gt 1) { $pre = $base[1..($base.Count - 1)] }

Write-Host '== 建构建 venv ==' -ForegroundColor Cyan
if (Test-Path -LiteralPath $VenvDir) {
    Write-Host "   复用 $VenvDir"
} else {
    $code = Invoke-Native $exe ($pre + @('-m', 'venv', $VenvDir))
    if ($code -ne 0) { throw "建 venv 失败 (exit $code)" }
}
if (-not (Test-Path -LiteralPath $VenvPy)) { throw "venv 里没有 python：$VenvPy" }

Write-Host '== 装 requests + pyinstaller ==' -ForegroundColor Cyan
$code = Invoke-Native $VenvPy @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet')
if ($code -ne 0) { throw "升级 pip 失败 (exit $code)" }
$code = Invoke-Native $VenvPy @('-m', 'pip', 'install', 'requests', 'pyinstaller', '--quiet')
if ($code -ne 0) { throw "装依赖失败 (exit $code)" }

$null = Invoke-Native $VenvPy @(
    '-c',
    "import PyInstaller, tkinter, requests, sys; print('   pyinstaller', PyInstaller.__version__, '| tk', tkinter.TkVersion, '| requests', requests.__version__); print('  ', sys.version.split()[0], sys.executable)"
)

Write-Host '== 构建 ==' -ForegroundColor Cyan
Push-Location $RepoRoot
try {
    $pyi = @('-m', 'PyInstaller', '--noconfirm')
    if (-not $NoClean) { $pyi += '--clean' }
    $pyi += (Resolve-Path -LiteralPath $SpecPath).Path
    $code = Invoke-Native $VenvPy $pyi
    if ($code -ne 0) { throw "PyInstaller 失败 (exit $code)" }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $OutDir)) { throw "构建成功但没找到产物目录：$OutDir" }

$bytes = (Get-ChildItem -LiteralPath $OutDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ''
Write-Host '== 产物 ==' -ForegroundColor Green
Get-ChildItem -LiteralPath $OutDir -File |
    ForEach-Object { Write-Host ("   {0,-22} {1,12:N0} B" -f $_.Name, $_.Length) }
Write-Host ("   整个目录 {0:N1} MB —— 分发时整个目录一起打包，不能只拷 exe" -f ($bytes / 1MB))
Write-Host ''
Write-Host "   双击：$OutDir\relaycheck-gui.exe"

if ($Test) {
    Write-Host ''
    Write-Host '== 端到端比对（源码 CLI / 引擎 exe / 窗口 exe）==' -ForegroundColor Cyan
    $code = Invoke-Native $VenvPy @((Join-Path $PSScriptRoot 'e2e_bundle.py'))
    if ($code -ne 0) { throw "端到端比对失败 (exit $code)" }
    Write-Host '   全部通过' -ForegroundColor Green
}
