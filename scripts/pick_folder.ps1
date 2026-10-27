param(
    [string]$InitialPath = ""
)

Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "Выберите папку для загрузок"
$dialog.ShowNewFolderButton = $true
if ($InitialPath -and (Test-Path -LiteralPath $InitialPath)) {
    $dialog.SelectedPath = $InitialPath
}

if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $dialog.SelectedPath
}
