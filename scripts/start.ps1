param([switch]$NoBrowser)
. "$PSScriptRoot\common.ps1"
if (-not (Test-Path -LiteralPath $pythonPath)) { & "$PSScriptRoot\install.ps1" }
$health = Get-ServiceHealth
if ($health) {
    if ($health.service -ne "video-downloader") { throw "Порт 8877 занят другим сервисом." }
    if (-not $NoBrowser) { Start-Process $baseUrl }
    Write-Host "Загрузчик уже работает: $baseUrl"
    exit 0
}
$runtimeDir = Join-Path $projectRoot "run"
$logsDir = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Force -Path $runtimeDir, $logsDir | Out-Null
$process = Start-Process -FilePath $pythonPath -ArgumentList @("run.py") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logsDir "service.out.log") -RedirectStandardError (Join-Path $logsDir "service.err.log")
$process.Id | Set-Content -LiteralPath (Join-Path $runtimeDir "service.pid")
for ($index = 0; $index -lt 60; $index++) {
    Start-Sleep -Milliseconds 500
    $health = Get-ServiceHealth
    if ($health -and $health.service -eq "video-downloader") {
        if (-not $NoBrowser) { Start-Process $baseUrl }
        Write-Host "Загрузчик запущен: $baseUrl"
        exit 0
    }
    $process.Refresh()
    if ($process.HasExited) { break }
}
throw "Не удалось запустить сервис. Посмотрите logs\service.err.log."
