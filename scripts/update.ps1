. "$PSScriptRoot\common.ps1"
if (-not (Test-Path -LiteralPath $pythonPath)) { & "$PSScriptRoot\install.ps1"; exit }
Stop-Downloader
& $pythonPath -m pip install --disable-pip-version-check --upgrade "yt-dlp[default]"
if ($LASTEXITCODE -ne 0) { throw "Обновление не удалось. Проверьте сеть и запустите обновление ещё раз." }
& $pythonPath "$PSScriptRoot\doctor.py"
if ($LASTEXITCODE -ne 0) { throw "Проверка компонентов не прошла." }
Write-Host "Загрузчик обновлён. Проекты, настройки и недокачанные файлы сохранены."
& "$PSScriptRoot\start.ps1"
