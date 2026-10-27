param([switch]$Plan)
. "$PSScriptRoot\common.ps1"
if ($Plan) {
    Write-Host "План: Python 3.11+; локальное окружение .venv; зависимости requirements.txt; FFmpeg/FFprobe; Node.js 22+; диагностика."
    Write-Host "Проекты, настройки и записи сохраняются. Whisper, модели и GPU не нужны."
    exit 0
}
Push-Location $projectRoot
try {
    Stop-Downloader
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        $basePython = Find-DownloaderPython
        if (-not $basePython) {
            if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { throw "Установите Python 3.11+ с python.org, затем повторите установку." }
            & winget.exe install --id Python.Python.3.13 --exact --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { throw "Не удалось установить Python. Установите Python 3.11+ вручную." }
            Update-TaskPath
            $basePython = Find-DownloaderPython
        }
        if (-not $basePython) { throw "Python 3.11+ не найден. Перезапустите терминал после установки Python и повторите запуск." }
        & $basePython -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Установите Python 3.11 или новее с python.org и повторите установку." }
    }
    & $pythonPath -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) { throw "Нужен Python 3.11 или новее. Установите его и пересоздайте только окружение .venv загрузчика." }
    & $pythonPath -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости. Проверьте сеть и повторите запуск установщика." }
    foreach ($component in @(@{Name="ffmpeg.exe";Package="Gyan.FFmpeg"}, @{Name="node.exe";Package="OpenJS.NodeJS.LTS"})) {
        if (-not (Get-Command $component.Name -ErrorAction SilentlyContinue)) {
            if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
                throw "Не найден $($component.Name). Установите FFmpeg и Node.js 22+ и повторите проверку."
            }
            & winget.exe install --id $component.Package --exact --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { throw "Не удалось установить $($component.Package). Установите компонент вручную." }
            Update-TaskPath
        }
    }
    & $pythonPath scripts\doctor.py
    if ($LASTEXITCODE -ne 0) { throw "Некоторые компоненты не готовы. См. результат проверки выше." }
    Write-Host "Загрузчик установлен. Запустите «Запустить загрузчик.bat»."
} finally { Pop-Location }
