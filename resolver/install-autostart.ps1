# 把 media-resolver 注册为「登录时自动启动」的计划任务。
#
# 用法（在 powershell 里以当前用户身份运行即可，无需管理员）：
#   powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
#
# 卸载：
#   powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1 -Uninstall

param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$taskName = 'media-resolver'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "已删除计划任务 $taskName"
    } else {
        Write-Host "计划任务 $taskName 不存在"
    }
    return
}

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) {
    throw '没有找到 node，请先安装 Node.js 并确保它在 PATH 中。'
}

if (-not (Test-Path (Join-Path $scriptDir 'config.json'))) {
    throw "缺少 $scriptDir\config.json，请先复制 config.example.json 并填写 server / token。"
}

$action = New-ScheduledTaskAction -Execute $node -Argument 'agent.js' -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# 每 5 分钟检查一次：如果进程已经退出（崩溃 / 被结束 / 休眠后掉线），任务会自动重新拉起。
# 进程还活着时由 MultipleInstances=IgnoreNew 保证不会重复启动。
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($trigger, $watchdog) -Settings $settings -Force | Out-Null
Write-Host "已注册计划任务 $taskName（登录时启动 + 每 5 分钟守护自启）"
Write-Host "立即启动： Start-ScheduledTask -TaskName $taskName"
Write-Host "查看日志： Get-Content '$scriptDir\resolver.log' -Tail 30"
