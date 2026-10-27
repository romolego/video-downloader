$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$baseUrl = "http://127.0.0.1:8877"

function Update-TaskPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$env:Path;$machinePath;$userPath"
    $vendorBin = Join-Path $projectRoot "vendor\bin"
    if (Test-Path -LiteralPath $vendorBin) { $env:Path = "$vendorBin;$env:Path" }
}

function Get-ServiceHealth {
    try { return Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2 } catch { return $null }
}

function Find-DownloaderPython {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $candidate = & $launcher.Source -3 -c "import sys; ok = sys.version_info >= (3, 11); print(sys.executable) if ok else None; sys.exit(0 if ok else 1)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidate) { return ($candidate | Select-Object -Last 1).Trim() }
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notlike '*\Microsoft\WindowsApps\*') {
        $candidate = & $pythonCommand.Source -c "import sys; ok = sys.version_info >= (3, 11); print(sys.executable) if ok else None; sys.exit(0 if ok else 1)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidate) { return ($candidate | Select-Object -Last 1).Trim() }
    }
    return $null
}

function Stop-Downloader {
    $health = Get-ServiceHealth
    if ($health -and $health.service -eq "video-downloader") {
        Invoke-RestMethod -Uri "$baseUrl/api/shutdown" -Method Post -Headers @{"X-Downloader-Client"="local"} -TimeoutSec 5 | Out-Null
        for ($index = 0; $index -lt 40; $index++) {
            Start-Sleep -Milliseconds 500
            if (-not (Get-ServiceHealth)) { return }
        }
        throw "Сервис ещё завершает запись файлов. Повторите через несколько секунд."
    }
}
Update-TaskPath
