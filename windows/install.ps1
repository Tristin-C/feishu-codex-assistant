$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$AppName = "FeishuCodexBridge"
$TaskName = "FeishuCodexBridge"
$Source = $PSScriptRoot
$AppDir = Join-Path $env:LOCALAPPDATA $AppName

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Find-Command([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

Write-Host "飞书 x Codex 一键安装" -ForegroundColor Green
Write-Host "安装目录：$AppDir"

Write-Step "检查 Windows 组件"
if (-not (Find-Command winget.exe)) {
    throw "未找到 winget。请先在 Microsoft Store 更新“应用安装程序”。"
}
$pythonReady = $false
if (Find-Command py.exe) {
    & py.exe -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
    $pythonReady = ($LASTEXITCODE -eq 0)
}
if (-not $pythonReady) {
    Write-Step "安装 Python 3.12"
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
}
$node = Find-Command node.exe
if (-not $node) {
    Write-Step "安装 Node.js LTS"
    winget install --id OpenJS.NodeJS.LTS -e --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
}
$npm = Find-Command npm.cmd
if (-not $npm) { throw "Node.js 安装后仍未找到 npm，请重启 Windows 后重试。" }

$larkCli = Join-Path $env:APPDATA "npm\lark-cli.cmd"
if (-not (Test-Path $larkCli)) {
    Write-Step "安装飞书 CLI"
    & $npm install -g "@larksuite/cli@1.0.97"
    if ($LASTEXITCODE -ne 0) { throw "飞书 CLI 安装失败" }
}

Write-Step "复制应用文件"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item (Join-Path $Source "app\*") $AppDir -Recurse -Force
Copy-Item (Join-Path $Source "run_bridge.vbs") $AppDir -Force
Copy-Item (Join-Path $Source "setup_gui.py") $AppDir -Force
Copy-Item (Join-Path $Source "uninstall.ps1") $AppDir -Force

Write-Step "创建 Python 环境"
$pythonExe = (& py.exe -3.12 -c "import sys; print(sys.executable)").Trim()
if (-not $pythonExe -or -not (Test-Path $pythonExe)) {
    throw "无法定位 Python 3.12"
}
if (-not (Test-Path (Join-Path $AppDir ".venv\Scripts\python.exe"))) {
    & $pythonExe -m venv (Join-Path $AppDir ".venv")
}
$venvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install "openai-codex==0.163.0"
if ($LASTEXITCODE -ne 0) { throw "Codex SDK 安装失败" }

Write-Step "注册开机启动"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$AppDir\run_bridge.vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Feishu Codex Bridge" -RunLevel Limited -Force | Out-Null

Write-Step "打开配置向导"
$env:HANDOFF_LARK_CLI = $larkCli
$setup = Join-Path $AppDir "setup_gui.py"
Start-Process -FilePath (Join-Path $AppDir ".venv\Scripts\pythonw.exe") -ArgumentList "`"$setup`"" -WorkingDirectory $AppDir

Write-Host ""
Write-Host "配置向导已打开。完成后可直接关闭本窗口。" -ForegroundColor Green

